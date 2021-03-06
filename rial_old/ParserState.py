import re
import threading
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from llvmlite import ir
from llvmlite.ir import BaseStructType

from rial.builtin_type_to_llvm_mapper import map_type_to_llvm
from rial.compilation_manager import CompilationManager
from rial.concept.name_mangler import mangle_global_name
from rial.log import log_fail
from rial.metadata.FunctionDefinition import FunctionDefinition
from rial.metadata.RIALFunction import RIALFunction
from rial.metadata.RIALIdentifiedStructType import RIALIdentifiedStructType
from rial.metadata.RIALModule import RIALModule
from rial.rial_types.RIALAccessModifier import RIALAccessModifier
from rial.rial_types.RIALVariable import RIALVariable


class ParserState:
    cached_functions: Dict[str, RIALFunction]
    cached_struct_modules: Dict[str, RIALModule]
    implemented_functions: List[str]
    threadLocalModule: threading.local
    threadLocalLLVMGen: threading.local
    builtin_types: Dict[str, Dict[str, RIALFunction]]

    def __init__(self):
        raise PermissionError()

    @staticmethod
    def init():
        ParserState.implemented_functions = list()
        ParserState.threadLocalModule = threading.local()
        ParserState.threadLocalLLVMGen = threading.local()
        ParserState.cached_functions = dict()
        ParserState.cached_struct_modules = dict()
        ParserState.builtin_types = dict()

    @staticmethod
    def set_module(module: RIALModule):
        ParserState.threadLocalModule.module = module

    @classmethod
    def module(cls) -> RIALModule:
        return cls.threadLocalModule.module

    @staticmethod
    def set_llvmgen(llvmgen):
        ParserState.threadLocalLLVMGen.llvmgen = llvmgen

    @classmethod
    def llvmgen(cls):
        return cls.threadLocalLLVMGen.llvmgen

    @staticmethod
    def add_dependency_and_wait(module_name: str):
        if Path(CompilationManager.path_from_mod_name(module_name)).exists():
            if module_name not in ParserState.module().dependencies:
                ParserState.module().dependencies.append(module_name)
            CompilationManager.request_module(module_name)
            CompilationManager.wait_for_module_compiled(module_name)
            return True

        return False

    @staticmethod
    def search_structs(name: str) -> Optional[RIALIdentifiedStructType]:
        # Does a global search.
        return ParserState.module().context.get_identified_type_if_exists(name)

    @staticmethod
    def find_global(name: str) -> Optional[RIALVariable]:
        # Search with just its name
        glob: RIALVariable = ParserState.module().get_rial_variable(name)

        # Search with module specifier
        if glob is None:
            glob = ParserState.module().get_rial_variable(mangle_global_name(ParserState.module().name, name))

        # Go through usings to find it
        if glob is None:
            globs_found: List[Tuple] = list()
            for using in ParserState.module().dependencies:
                path = CompilationManager.path_from_mod_name(using)
                if path not in CompilationManager.modules:
                    continue
                module = CompilationManager.modules[path]

                gl = module.get_rial_variable(name)

                if gl is None:
                    gl = module.get_rial_variable(mangle_global_name(using, name))

                if gl is not None:
                    globs_found.append((using, gl))

            if len(globs_found) == 0:
                return None

            if len(globs_found) > 1:
                raise KeyError(name)

            glob = globs_found[0][1]

            if glob.access_modifier == RIALAccessModifier.PRIVATE:
                raise PermissionError(name)

            if glob.access_modifier == RIALAccessModifier.INTERNAL and glob.backing_value.parent.split(':')[0] != \
                    ParserState.module().name.split(':')[
                        0]:
                raise PermissionError(name)

        return glob

    @staticmethod
    def find_function(full_function_name: str, rial_arg_types: List[str] = None) -> Optional[RIALFunction]:
        # Check by canonical name if we got args to check
        if rial_arg_types is not None:
            func = ParserState.module().get_function(full_function_name)

            # If couldn't find it, iterate through usings and try to find function
            if func is None:
                functions_found: List[Tuple[str, RIALFunction]] = list()

                for use in ParserState.module().dependencies:
                    path = CompilationManager.path_from_mod_name(use)
                    if path not in CompilationManager.modules:
                        continue
                    module = CompilationManager.modules[path]

                    function = module.get_function(full_function_name)

                    if function is None:
                        continue
                    functions_found.append((use, function,))

                # Check each function if the arguments match the passed arguments
                if len(functions_found) > 1:
                    for tup in functions_found:
                        function = tup[1]
                        matches = True

                        for i, arg in enumerate(function.definition.rial_args):
                            if arg[0] != rial_arg_types[i]:
                                matches = False
                                break
                        if matches:
                            func = function
                            break

                # Check for number of functions found
                elif len(functions_found) == 1:
                    func = functions_found[0][1]

            if func is not None:
                # Function is in current module and only a declaration, safe to assume that it's a redeclared function
                # from another module or originally declared in this module anyways
                if func.module.name != ParserState.module().name and not func.is_declaration:
                    # Function cannot be accessed if:
                    #   - Function is not public and
                    #   - Function is internal but not in same TLM (top level module) or
                    #   - Function is private but not in same module
                    func_def: FunctionDefinition = func.definition
                    if func_def.access_modifier != RIALAccessModifier.PUBLIC and \
                            ((func_def.access_modifier == RIALAccessModifier.INTERNAL and
                              func.module.name.split(':')[0] != ParserState.module().name.split(':')[0]) or
                             (func_def.access_modifier == RIALAccessModifier.PRIVATE and
                              func.module.name != ParserState.module().name)):
                        log_fail(
                            f"Cannot access method {full_function_name} in module {func.module.name}!")
                        return None

        # Try to find function in current module
        func: RIALFunction = ParserState.module().get_global_safe(full_function_name)

        # Try to find function in current module with module specifier
        if func is None:
            func = ParserState.module().get_global_safe(f"{ParserState.module().name}:{full_function_name}")

        # If func isn't in current module
        if func is None:
            # If couldn't find it, iterate through usings and try to find function
            if func is None:
                functions_found: List[Tuple[str, RIALFunction]] = list()

                for use in ParserState.module().dependencies:
                    path = CompilationManager.path_from_mod_name(use)
                    if path not in CompilationManager.modules:
                        continue
                    module = CompilationManager.modules[path]

                    function = module.get_global_safe(full_function_name)

                    if function is None:
                        function = module.get_global_safe(f"{use}:{full_function_name}")

                    if function is None:
                        continue
                    functions_found.append((use, function,))

                if len(functions_found) > 1:
                    log_fail(f"Function {full_function_name} has been declared multiple times!")
                    log_fail(f"Specify the specific function to use by adding the namespace to the function call")
                    log_fail(f"E.g. {functions_found[0][0]}:{full_function_name}()")
                    return None

                # Check for number of functions found
                if len(functions_found) == 1:
                    func = functions_found[0][1]

            if func is not None:
                # Function is in current module and only a declaration, safe to assume that it's a redeclared function
                # from another module or originally declared in this module anyways
                if func.module.name != ParserState.module().name and not func.is_declaration:
                    # Function cannot be accessed if:
                    #   - Function is not public and
                    #   - Function is internal but not in same TLM (top level module) or
                    #   - Function is private but not in same module
                    func_def: FunctionDefinition = func.definition
                    if func_def.access_modifier != RIALAccessModifier.PUBLIC and \
                            ((func_def.access_modifier == RIALAccessModifier.INTERNAL and
                              func.module.name.split(':')[0] != ParserState.module().name.split(':')[0]) or
                             (func_def.access_modifier == RIALAccessModifier.PRIVATE and
                              func.module.name != ParserState.module().name)):
                        log_fail(
                            f"Cannot access method {full_function_name} in module {func.module.name}!")
                        return None

        return func

    @staticmethod
    def find_struct(struct_name: str) -> Optional[RIALIdentifiedStructType]:
        # Search with name
        struct = ParserState.search_structs(struct_name)

        # Search with current module specifier
        if struct is None:
            struct = ParserState.search_structs(f"{ParserState.module().name}:{struct_name}")

        # Iterate through usings
        if struct is None:
            structs_found: List[Tuple] = list()
            for using in ParserState.module().dependencies:
                s = ParserState.search_structs(f"{using}:{struct_name}")

                if s is not None:
                    structs_found.append((using, s))
            if len(structs_found) == 0:
                return None

            if len(structs_found) > 1:
                log_fail(f"Multiple declarations found for {struct_name}")
                log_fail(f"Specify one of them by using {structs_found[0][0]}:{struct_name} for example")
                return None
            struct = structs_found[0][1]

        return struct

    @staticmethod
    def map_type_to_llvm(name: str):
        llvm_type = ParserState.map_type_to_llvm_no_pointer(name)

        # Create pointer for struct and array
        if llvm_type is not None and (isinstance(llvm_type, BaseStructType) or isinstance(llvm_type, ir.ArrayType)):
            llvm_type = ir.PointerType(llvm_type)

        return llvm_type

    @staticmethod
    def map_type_to_llvm_no_pointer(name: str):
        llvm_type = map_type_to_llvm(name)

        # Check if builtin type
        if llvm_type is None:
            llvm_type = ParserState.find_struct(name)

            if llvm_type is None:
                # Arrays
                match = re.match(r"^([^\[]+)\[([0-9]+)?\]$", name)

                if match is not None:
                    ty = match.group(1)
                    count = match.group(2)
                    if count is not None:
                        return ir.ArrayType(ParserState.map_type_to_llvm(ty), int(count))
                    else:
                        return ParserState.map_type_to_llvm(ty).as_pointer()

                # Function type
                match = re.match(r"^([^(]+)\(([^,)]+\s*,?\s*)*\)$", name)

                if match is not None:
                    return_type = ""
                    arg_types = list()
                    var_args = False
                    for i, group in enumerate(match.groups()):
                        if i == 0:
                            return_type = group.strip()
                        elif group == "...":
                            var_args = True
                        elif group is None:
                            break
                        else:
                            arg_types.append(group.strip())

                    return ir.FunctionType(ParserState.map_type_to_llvm(return_type),
                                           [ParserState.map_type_to_llvm(arg) for arg in arg_types],
                                           var_args)

                log_fail(f"Referenced unknown type {name}")
                return None

        return llvm_type
