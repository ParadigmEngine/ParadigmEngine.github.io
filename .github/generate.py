import os
import json
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import subprocess
import pathlib
import tempfile

CURRENT_DIR = pathlib.Path(__file__).parent.resolve()
ROOT_DIR = pathlib.Path(__file__).parent.parent.resolve()
SHA1_LENGTH = 40

GENERATOR_VERSION = 1

PYTHON_COMMAND = "python"


def _remove_directory(directory, include_self=False):
    if not pathlib.Path.exists(directory):
        return
    for entry in directory.iterdir():
        if entry.is_file():
            pathlib.Path.unlink(entry)
        elif entry.is_dir():
            _remove_directory(entry, include_self=True)
        else:
            raise Exception(f"Unhandled path type: {entry}")
    if include_self:
        pathlib.Path.rmdir(directory)


def _git_entry_to_dict(values, prefix=None):
    results = dict()
    for value in values:
        name = (value[1].split("^", maxsplit=1)[0]).lstrip("\t ")
        if prefix is not None:
            name = name[len(prefix) :] if name.startswith(prefix) else name
        if "^" in value[1] or name not in results:
            results[name] = value[0]
    return results


def run_command(directory=None, command=[], verbose=False):
    if verbose:
        print(
            f"executing '{' '.join(os.fspath(comm) if isinstance(comm, pathlib.Path) else comm for comm in command)}' in '{directory or CURRENT_DIR}'"
        )
    output_lines = subprocess.Popen(
        command, stdout=subprocess.PIPE, cwd=directory, stderr=subprocess.DEVNULL
    ).stdout.readlines()
    return [output.decode("utf-8").strip() for output in output_lines]


class Info:
    def __init__(self, info_file=None, verbose=False, regenerate=False) -> None:
        # tags and branches are a dict(name: sha1), where sha1 indicates the sha1 that was generated for that tag/branch
        self.tags = dict()
        self.branches = dict()
        self.version = GENERATOR_VERSION
        self.__file = info_file
        self.generator = None
        if not regenerate and info_file is not None and pathlib.Path.exists(info_file):
            if verbose:
                print(f"loading cache at '{info_file}'")
            with pathlib.Path.open(info_file.resolve(), "rb") as file:
                data = json.load(file)
                self.version = data.get("version")
                if self.version == GENERATOR_VERSION:
                    self.tags = data.get("tags")
                    self.branches = data.get("branches")
                    self.generator = data.get("generator")
                else:
                    raise Exception(
                        "Unhandled version, the cache contains version '{self.version}' while the generator is '{GENERATOR_VERSION}', and no migration was supported."
                    )

    def save(self) -> None:
        with open(self.__file, "w") as file:
            data = {
                "version": GENERATOR_VERSION,
                "tags": self.tags,
                "branches": self.branches,
                "generator": self.generator,
            }
            file.write(json.dumps(data, sort_keys=True, indent=4))


class Repository:
    def __init__(self, url, verbose=False) -> None:
        self.__temp = None
        self.name = url.rsplit("/", maxsplit=1)[-1].rstrip(".git")
        self.url = url
        self.__temp = tempfile.TemporaryDirectory()
        self.verbose = verbose

        self.tags = _git_entry_to_dict(
            [
                (tag[:SHA1_LENGTH], tag[SHA1_LENGTH:])
                for tag in run_command(
                    command=["git", "ls-remote", "--tags", url], verbose=self.verbose
                )
            ],
            "refs/tags/",
        )
        self.branches = _git_entry_to_dict(
            [
                (branch[:SHA1_LENGTH], branch[SHA1_LENGTH:])
                for branch in run_command(
                    command=["git", "ls-remote", "--heads", url], verbose=self.verbose
                )
            ],
            "refs/heads/",
        )
        self.path = pathlib.Path(self.__temp.name)

    def __del__(self) -> None:
        if self.__temp is not None:
            self.__temp.cleanup()
        pass

    def checkout(self, branch) -> None:
        if self.verbose:
            print(f"checking out {self.name}: {branch}")
        path = pathlib.Path.joinpath(self.path, branch)
        if pathlib.Path.exists(path):
            raise Exception(f"duplicate tag/branch names, please verify '{branch}'")
        pathlib.Path.mkdir(path, parents=True)
        run_command(
            path,
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                self.url,
                "--branch",
                branch,
                "--single-branch",
                ".",
            ],
            self.verbose,
        )
        return path


def generate(repository, generator, info=Info(), verbose=False):
    tags = repository.tags
    branches = repository.branches

    generator_sha1 = [
        line[:SHA1_LENGTH]
        for line in run_command(
            command=["git", "ls-remote", generator, "HEAD"], verbose=verbose
        )
    ]
    assert len(generator_sha1) == 1 and len(generator_sha1[0]) == SHA1_LENGTH
    generator_sha1 = generator_sha1[0]
    if info.generator == generator_sha1:
        if verbose:
            print("pruning pre-existing tags and branches")
        # prune pre-existing
        [
            _remove_directory(pathlib.Path.joinpath(ROOT_DIR, tag), include_self=True)
            for tag in info.tags
            if tag not in repository.tags
        ]
        [
            _remove_directory(
                pathlib.Path.joinpath(ROOT_DIR, branch), include_self=True
            )
            for branch in info.branches
            if branch not in repository.branches
        ]

        tags = {
            tag: repository.tags[tag]
            for tag in repository.tags
            if (tag not in info.tags or info.tags[tag] != repository.tags[tag])
        }
        branches = {
            branch: repository.branches[branch]
            for branch in repository.branches
            if (
                branch not in info.branches
                or info.branches[branch] != repository.branches[branch]
            )
        }
    elif verbose:
        print(
            f"generator was updated [old:'{info.generator}' new:'{generator_sha1}'], regenerating all"
        )

    def generate_for(generator_dir, repository, name, sha1, verbose=False):
        if verbose:
            print(f"generating {name} - {sha1}")
        path = repository.checkout(name)
        output_dir = pathlib.Path.joinpath(ROOT_DIR, "docs", name)
        if pathlib.Path.exists(output_dir):
            _remove_directory(output_dir, True)

        if output_dir.parent != ROOT_DIR:
            pathlib.Path.mkdir(output_dir.parent, parents=True, exist_ok=True)

        doxyfile = pathlib.Path.joinpath(path, "tools", "doxyfile")
        if not pathlib.Path.exists(doxyfile):
            pathlib.Path.mkdir(doxyfile.parent, parents=True, exist_ok=True)
            doxyfile.write_bytes(
                pathlib.Path.joinpath(CURRENT_DIR, "doxyfile").read_bytes()
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            documentation_path = generator_dir.joinpath("documentation")
            css_path = generator_dir.joinpath("css")
            run_command(
                directory=css_path,
                command=[
                    PYTHON_COMMAND,
                    "postprocess.py",
                    "m-dark.css",
                    "m-documentation.css",
                    "-o",
                    "m-dark+documentation.compiled.css",
                ],
                verbose=verbose,
            )
            run_command(
                directory=documentation_path,
                command=[
                    PYTHON_COMMAND,
                    "doxygen.py",
                    doxyfile.resolve(),
                    "--output-dir",
                    temp_dir,
                ],
                verbose=verbose,
            )
            pathlib.Path.rename(pathlib.Path(temp_dir), target=output_dir)

    if len(tags) != 0 or len(branches) != 0:
        if verbose:
            print(f"generating {len(tags)} tags and {len(branches)} branches")
        with tempfile.TemporaryDirectory() as generator_root:
            if verbose:
                print(f"checking out the generator...")
            run_command(
                directory=generator_root,
                command=["git", "clone", generator, "--depth", "1", "."],
                verbose=verbose,
            )

            [
                generate_for(
                    pathlib.Path(generator_root),
                    repository,
                    key,
                    tags[key],
                    verbose=verbose,
                )
                for key in tags
            ]
            [
                generate_for(
                    pathlib.Path(generator_root),
                    repository,
                    key,
                    branches[key],
                    verbose=verbose,
                )
                for key in branches
            ]

            # generate selector dropdown
            files = dict()
            mappings = dict()
            for name in tags | branches:
                dir = ROOT_DIR.joinpath("docs", name)
                files[name] = {
                    entry.relative_to(dir)
                    for entry in dir.iterdir()
                    if entry.is_file() and entry.suffix == ".html"
                }

            for key in files:
                other_keys = [other for other in files if key != other]

                for file in files[key]:
                    if file in mappings:
                        continue
                    mappings[file] = {
                        other: pathlib.Path.joinpath(
                            pathlib.Path(other),
                            (
                                file
                                if file in files[other]
                                else pathlib.Path("index.html")
                            ),
                        )
                        for other in other_keys
                    }
                    mappings[file][key] = pathlib.Path.joinpath(pathlib.Path(key), file)

            for tag in files:
                prefix = "../" * (tag.count("/") + 1)
                other_keys = sorted([other for other in files if tag != other])
                for file in files[tag]:
                    with ROOT_DIR.joinpath("docs", tag, file).open("r+") as f:
                        text = f.read()
                        f.seek(0)
                        index = text.find('<use href="#m-doc-search-icon-path" />')
                        assert index != -1
                        sub_index = text[index:].find("</li>")
                        assert sub_index != -1
                        index = index + sub_index + 5
                        otheroptions = "".join(
                            f'<option value="{ prefix + str(mappings[file][othertag].as_posix())}">{othertag}</option>'
                            for othertag in other_keys
                        )
                        text = (
                            text[:index]
                            + '<li style="line-height: 2.5rem;"><select name="branch/tag" id="ref-select" onchange="location = this.value;" style="border-width: 0 0 0.25rem 0;">'
                            + f'<option value="{prefix + str(mappings[file][tag].as_posix())}">{tag}</option>'
                            + otheroptions
                            + "</select></li>"
                            + text[index:]
                        )
                        f.write(text)
                        f.truncate()
    elif verbose:
        print(f"everything was pruned, nothing to generate")

    info.tags = repository.tags
    info.branches = repository.branches
    info.generator = generator_sha1
    info.save()


if __name__ == "__main__":
    parser = ArgumentParser(
        description="Conditionally generate, or update the documentation based on new tags, or generator changes.",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repository",
        default="https://github.com/JessyDL/paradigm.git",
        help="git url for the repo to generate for",
    )
    parser.add_argument(
        "--generator",
        default="https://github.com/JessyDL/m.css.git",
        help="git url for the generator to use",
    )
    parser.add_argument(
        "--cache",
        default=pathlib.Path.joinpath(CURRENT_DIR, "cache.json"),
        help="Location where to store previous generation info.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="forcibly regenerate the entire cache and results.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Controls verbosity of actions."
    )
    args = parser.parse_args()

    python = run_command(command=["python3", "--version"])
    if len(python) == 0 or not python[0].startswith("Python 3"):
        python = run_command(command=["python", "--version"])
        if len(python) == 0 or not python[0].startswith("Python 3"):
            raise Exception("could not establish python version")
        else:
            PYTHON_COMMAND = "python"
    else:
        PYTHON_COMMAND = "python3"

    generate(
        repository=Repository(args.repository, args.verbose),
        generator=args.generator,
        info=Info(args.cache, regenerate=args.force),
        verbose=args.verbose,
    )
