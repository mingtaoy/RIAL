from typing import Optional, Union, Tuple, List, Literal, Dict

from llvmlite import ir
from llvmlite.ir import Module, IRBuilder, Function, AllocaInstr, Branch, Block, FunctionType, Type, VoidType, Value, \
    PointerType

from rial.LLVMBlock import LLVMBlock, create_llvm_block
from rial.LLVMStruct import LLVMStruct
from rial.rial_types.RIALAccessModifier import RIALAccessModifier
from rial.rial_types.RIALVariable import RIALVariable


class LLVMGen:
    builder: Optional[IRBuilder]
    current_func: Optional[Function]
    conditional_block: Optional[LLVMBlock]
    end_block: Optional[LLVMBlock]
    current_block: Optional[LLVMBlock]
    module: Module
    current_struct: Optional[LLVMStruct]
    global_variables: Dict

    def __init__(self, module: Module):
        self.module = module
        self.current_block = None
        self.conditional_block = None
        self.end_block = None
        self.current_func = None
        self.builder = None
        self.current_struct = None
        self.global_variables = dict()

    def gen_integer(self, number: int, length: int):
        return ir.Constant(ir.IntType(length), number)

    def gen_string_lit(self, name: str, value: str):
        value = eval("'{}'".format(value))

        # TODO: Remove the always-added \0 once we don't need that anymore
        arr = bytearray(value.encode("utf-8") + b"\x00")
        const_char_arr = ir.Constant(ir.ArrayType(ir.IntType(8), len(arr)), arr)
        glob = ir.GlobalVariable(self.module, const_char_arr.type, name=name)
        glob.linkage = 'private'
        glob.global_constant = True
        glob.initializer = const_char_arr
        glob.unnamed_addr = True

        return glob

    def gen_addition(self, left, right):
        if isinstance(left.type, ir.IntType):
            return self.builder.add(left, right)

        if isinstance(left.type, ir.FloatType) or isinstance(left.type, ir.DoubleType):
            return self.builder.fadd(left, right)

        return None

    def gen_subtraction(self, left, right):
        if isinstance(left.type, ir.IntType):
            return self.builder.sub(left, right)

        if isinstance(left.type, ir.FloatType) or isinstance(left.type, ir.DoubleType):
            return self.builder.fsub(left, right)

        return None

    def gen_multiplication(self, left, right):
        if isinstance(left.type, ir.IntType):
            return self.builder.mul(left, right)

        if isinstance(left.type, ir.FloatType) or isinstance(left.type, ir.DoubleType):
            return self.builder.fmul(left, right)

        return None

    def gen_division(self, left, right):
        if isinstance(left.type, ir.IntType):
            return self.builder.sdiv(left, right)

        if isinstance(left.type, ir.FloatType) or isinstance(left.type, ir.DoubleType):
            return self.builder.fdiv(left, right)

        return None

    def gen_comparison(self, comparison: str, left, right):
        if isinstance(left.type, ir.IntType):
            return self.builder.icmp_signed(comparison, left, right)

        if isinstance(left.type, ir.FloatType) or isinstance(left.type, ir.DoubleType):
            return self.builder.fcmp_ordered(comparison, left, right)

        return None

    def gen_shorthand(self, variable, value, operation):
        loaded = self.builder.load(variable)
        mathed = None

        if operation == "+":
            mathed = self.gen_addition(loaded, value)
        elif operation == "-":
            mathed = self.gen_subtraction(loaded, value)
        elif operation == "*":
            mathed = self.gen_multiplication(loaded, value)
        elif operation == "/":
            mathed = self.gen_division(loaded, value)

        self.builder.store(mathed, variable)

        return mathed

    def declare_variable(self, identifier: str, variable_type, value, rial_type: str) -> Optional[AllocaInstr]:
        variable = self.current_block.get_named_value(identifier)

        if variable is not None:
            return None

        if isinstance(variable_type, PointerType) and isinstance(value.type, PointerType):
            variable = self.builder.alloca(variable_type.pointee)
            loaded_val = self.builder.load(value)
            self.builder.store(loaded_val, variable)
            variable.name = identifier
        else:
            variable = self.builder.alloca(variable_type)
            variable.name = identifier
            variable.set_metadata('type',
                                  self.module.add_metadata((rial_type,)))
            if value is not None:
                self.builder.store(value, variable)

        self.current_block.add_named_value(identifier, variable is None and value or variable)

        return variable

    def assign_to_variable(self, identifier: str, value):
        variable = self.current_block.get_named_value(identifier)

        if variable is None:
            return None

        self.builder.store(value, variable)

        return variable

    def create_block(self, block_name: str, parent: Optional[LLVMBlock] = None, sibling: Optional[LLVMBlock] = None) -> \
            Optional[LLVMBlock]:
        if parent is None and sibling is None and block_name != "entry" and len(self.current_func.basic_blocks) > 0:
            return None

        block = self.builder.append_basic_block(block_name)
        llvmblock = create_llvm_block(block, parent, sibling)

        return llvmblock

    def create_loop(self, base_block_name: str, parent: LLVMBlock):
        (conditional_llvm_block, body_llvm_block, end_llvm_block) = self.create_conditional_block(base_block_name,
                                                                                                  parent)

        self.conditional_block = conditional_llvm_block
        self.end_block = end_llvm_block

        return conditional_llvm_block, body_llvm_block, end_llvm_block,

    def create_conditional_block(self, base_block_name: str, parent: LLVMBlock) -> Tuple[
        LLVMBlock, LLVMBlock, LLVMBlock]:
        # Create three blocks, one condition, one body, and one after the loop
        conditional_llvm_block = self.create_block(f"{base_block_name}.condition", parent, None)
        body_llvm_block = self.create_block(f"{base_block_name}.body", conditional_llvm_block, None)
        end_llvm_block = self.create_block(f"{base_block_name}.end", None, parent)

        return conditional_llvm_block, body_llvm_block, end_llvm_block,

    def create_conditional_block_with_else(self, base_block_name: str, parent: LLVMBlock):
        (conditional_block, body_block, else_block) = self.create_conditional_block(base_block_name, parent)

        # Transform end block into else block
        else_block.block.name = f"{base_block_name}.else"
        else_block.sibling = None
        else_block.parent = conditional_block

        # Create new end block
        end_block = self.create_block(f"{base_block_name}.if_else.end", None, conditional_block.parent)

        return conditional_block, body_block, else_block, end_block

    def create_jump_if_not_exists(self, target_block: LLVMBlock) -> Optional[Branch]:
        if self.current_block.block.terminator is None:
            return self.builder.branch(target_block.block)

        return None

    def create_conditional_jump(self, condition, true_block: LLVMBlock, false_block: LLVMBlock):
        return self.builder.cbranch(condition, true_block.block, false_block.block)

    def create_jump(self, target_block: LLVMBlock):
        return self.builder.branch(target_block.block)

    def enter_block(self, llvmblock: LLVMBlock):
        self.current_block = llvmblock
        self.builder.position_at_start(self.current_block.block)

    def create_identified_struct(self, name: str, linkage: Union[Literal["internal"], Literal["external"]],
                                 rial_access_modifier: RIALAccessModifier,
                                 body: List[RIALVariable]):
        struct = self.module.context.get_identified_type(name)
        struct.set_body(*tuple([bod.llvm_type for bod in body]))
        llvm_struct = LLVMStruct(struct, name, rial_access_modifier)
        self.current_struct = llvm_struct

        # Create base constructor
        function_type = self.create_function_type(ir.PointerType(struct), [], False)
        func = self.create_function_with_type(f"{name}.constructor", function_type, linkage, "fastcc", [], True,
                                              rial_access_modifier, name, [])
        self_value = self.builder.alloca(struct, name="this")

        # Set initial values
        for i, bod in enumerate(body):
            loaded_var = self.builder.gep(self_value, [ir.Constant(ir.IntType(32), i), ir.Constant(ir.IntType(32), 0)])
            self.builder.store(bod.initial_value, loaded_var)

            # Store reference in llvm_struct
            llvm_struct.properties[bod.name] = (i, bod)

        # Return self
        self.builder.ret(self_value)
        llvm_struct.constructor = self.current_func

        return llvm_struct

    def finish_struct(self):
        self.current_struct = None
        self.current_func = None
        self.current_block = None

    def create_function_with_type(self, name: str, ty: FunctionType,
                                  linkage: Union[Literal["internal"], Literal["external"]],
                                  calling_convention: str,
                                  arg_names: List[str],
                                  generate_body: bool,
                                  rial_access_modifier: RIALAccessModifier,
                                  rial_return_type: str,
                                  rial_arg_types: List[str]):

        # Create function with specified linkage (internal -> module only)
        func = ir.Function(self.module, ty, name=name)
        func.linkage = linkage
        func.calling_convention = calling_convention

        if generate_body:
            func.set_metadata('function_definition',
                              self.module.add_metadata((rial_return_type, str(rial_access_modifier),)))

        # Set argument names
        for i, arg in enumerate(func.args):
            arg.name = arg_names[i]

        # Generate standard function body
        if generate_body:
            self.current_func = func

            # Create entry block
            bb = func.append_basic_block("entry")
            llvm_bb = create_llvm_block(bb)
            self.current_block = llvm_bb

            if self.builder is None:
                self.builder = IRBuilder(bb)

            self.builder.position_at_start(bb)

            # Allocate new variables for the passed arguments
            for i, arg in enumerate(func.args):
                # Don't copy variables that are a pointer
                if isinstance(arg.type, PointerType):
                    continue
                allocated_arg = self.builder.alloca(arg.type)
                self.builder.store(arg, allocated_arg)
                self.current_block.named_values[arg.name] = allocated_arg
                allocated_arg.set_metadata('type',
                                           self.module.add_metadata((rial_arg_types[i],)))

        return func

    def finish_current_block(self):
        if self.current_block.block.terminator is None:
            self.builder.position_at_end(self.current_block.block)
            self.builder.ret_void()

    def create_return_statement(self, statement):
        if isinstance(statement, VoidType):
            return self.builder.ret_void()

        return self.builder.ret(statement)

    def finish_loop(self):
        self.conditional_block = None
        self.end_block = None

    def create_function_type(self, llvm_return_type: Type, llvm_arg_types: List[Type], var_args: bool):
        return ir.FunctionType(llvm_return_type, tuple(llvm_arg_types), var_arg=var_args)
