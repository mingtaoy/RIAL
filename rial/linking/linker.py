from typing import List
import subprocess

from shutil import which
import shlex

from rial.platform.Platform import Platform


class Linker:
    @staticmethod
    def link_files(object_files: List[str], exe_file: str, print_link_command: bool, strip: bool):
        opts = Platform.get_link_options()
        opts.linker_object_args = object_files
        opts.linker_output_arg = exe_file

        if strip:
            opts.linker_pre_args.append('-Wl,--gc-sections')

        args = f"{opts.linker_executable} {print_link_command and '-v' or ''} {' '.join(opts.linker_pre_args)} {' '.join(opts.linker_object_args)} -o {opts.linker_output_arg} {' '.join(opts.linker_post_args)}"

        subprocess.run(shlex.split(args))

        if strip:
            strip_path = which('llvm-strip')

            if strip_path is None:
                strip_path = which('llvm-strip-8')

            if strip_path is not None:
                args = f"{strip_path} --strip-all {exe_file}"
                subprocess.run(shlex.split(args))
