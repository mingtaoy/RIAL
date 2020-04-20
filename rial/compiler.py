import multiprocessing
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import List, Dict

from llvmlite.binding import ModuleRef

from rial.ASTVisitor import ASTVisitor
from rial.FunctionDeclarationTransformer import FunctionDeclarationTransformer
from rial.ParserState import ParserState
from rial.PrimitiveASTTransformer import PrimitiveASTTransformer
from rial.StructDeclarationTransformer import StructDeclarationTransformer
from rial.compilation_manager import CompilationManager
from rial.concept.Postlexer import Postlexer
from rial.concept.parser import Lark_StandAlone
from rial.linking.linker import Linker
from rial.log import log_fail
from rial.platform.Platform import Platform
from rial.profiling import run_with_profiling, ExecutionStep


def compiler():
    path = CompilationManager.config.rial_path.joinpath("builtin").joinpath("start.rial")
    # path = source_path.joinpath("main.rial")

    if not path.exists():
        log_fail("Main file not found in source path!")
        sys.exit(1)

    threads = list()

    CompilationManager.files_to_compile.put(path)

    for i in range(multiprocessing.cpu_count()):
        t = threading.Thread(target=compile_file)
        t.daemon = True
        t.start()
        threads.append(t)

    CompilationManager.files_to_compile.join()

    modules: Dict[str, ModuleRef] = dict()

    for key, mod in CompilationManager.modules.items():
        modules[key] = CompilationManager.codegen.compile_ir(mod)

    object_files: List[str] = list()
    llvm_bitcode_files: List[str] = list()
    CompilationManager.codegen.generate_final_modules(list(modules.values()))

    for path in list(modules.keys()):
        mod = modules[path]

        if CompilationManager.config.raw_opts.print_ir:
            ir_file = str(CompilationManager.get_cache_path_str(path)).replace(".rial", ".ll")
            CompilationManager.codegen.save_ir(ir_file, mod)

        if CompilationManager.config.raw_opts.print_asm:
            asm_file = str(CompilationManager.get_cache_path_str(path)).replace(".rial", ".asm")
            CompilationManager.codegen.save_assembly(asm_file, mod)

        if not CompilationManager.config.raw_opts.use_object_files:
            llvm_bitcode_file = str(CompilationManager.get_cache_path_str(path)).replace(".rial", ".lbc")
            CompilationManager.codegen.save_llvm_bitcode(llvm_bitcode_file, mod)
            llvm_bitcode_files.append(llvm_bitcode_file)

        object_file = str(CompilationManager.get_cache_path_str(path)).replace(".rial", ".o")
        CompilationManager.codegen.save_object(object_file, mod)
        object_files.append(object_file)

    with run_with_profiling(CompilationManager.config.project_name, ExecutionStep.LINK_EXE):
        exe_path = str(CompilationManager.config.bin_path.joinpath(
            f"{CompilationManager.config.project_name}{Platform.get_exe_file_extension()}"))

        if CompilationManager.config.raw_opts.use_object_files:
            Linker.link_files(object_files, exe_path, CompilationManager.config.raw_opts.print_link_command,
                              CompilationManager.config.raw_opts.strip)
        else:
            output_path = str(CompilationManager.config.cache_path.joinpath("temp_total.ll"))
            Linker.link_lbc_files(llvm_bitcode_files, output_path)
            Linker.link_files([output_path], exe_path, CompilationManager.config.raw_opts.print_link_command,
                              CompilationManager.config.raw_opts.strip)


def compile_file():
    try:
        while True:
            path = CompilationManager.files_to_compile.get()

            if not Path(path).exists():
                log_fail(f"Could not find {path}")
                CompilationManager.finish_file(path)
                CompilationManager.files_to_compile.task_done()
                continue

            file = str(path).replace(str(CompilationManager.config.source_path), "")
            file = file.replace(str(CompilationManager.config.rial_path), "")
            module_name = CompilationManager.mod_name_from_path(file)

            if module_name.startswith("builtin") or module_name.startswith("std"):
                module_name = f"rial:{module_name}"
            else:
                module_name = CompilationManager.config.project_name + ":" + module_name
            module = CompilationManager.codegen.get_module(module_name, file.split('/')[-1],
                                                           str(CompilationManager.config.source_path))
            ParserState.reset_usings()
            ParserState.set_module(module)

            primitive_transformer = PrimitiveASTTransformer()
            function_declaration_transformer = FunctionDeclarationTransformer()
            struct_declaration_transformer = StructDeclarationTransformer()
            transformer = ASTVisitor()

            parser = Lark_StandAlone(transformer=primitive_transformer, postlex=Postlexer())

            with run_with_profiling(file, ExecutionStep.READ_FILE):
                with open(path, "r") as src:
                    contents = src.read()

            with run_with_profiling(file, ExecutionStep.PARSE_FILE):
                ast = parser.parse(contents)
                ast = struct_declaration_transformer.transform(ast)
                ast = function_declaration_transformer.visit(ast)

                # Declarations are all already collected so we can move on.
                CompilationManager.modules[str(path)] = module
                CompilationManager.finish_file(path)

                transformer.visit(ast)

            if CompilationManager.config.raw_opts.print_tokens:
                print(ast.pretty())

            CompilationManager.files_to_compile.task_done()
    except Exception as e:
        log_fail("Internal Compiler Error: ")
        log_fail(traceback.format_exc())
        os._exit(-1)
    finally:
        del parser
