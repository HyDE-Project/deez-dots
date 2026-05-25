# deez-dots

Deez dots is a CLI tool for managing dotfiles and dotfile deployments. Sooner this will be the backend implementation for [The HyDE Project](https://github.com/Hyde-Project/Hyde).


## Example Usage

```bash
./deez --version
./deez --help
./deez dots --config ./example/dots.toml --deploy
./deez deps --config ./example/dots.toml --check
./deez cache --list
```

You can also run via Python directly from the repo root:

```bash
python -m deez --version
python -m deez dots --config ./example/dots.toml --deploy
```

Or install directly from GitHub without using a curl-to-shell bootstrap:

With `pip`:

```bash
python -m pip install git+https://github.com/HyDE-Project/deez-dots.git
```

With `pipx`:

```bash
pipx install git+https://github.com/HyDE-Project/deez-dots.git
```

With `uv` as a persistent tool install:

```bash
uv tool install git+https://github.com/HyDE-Project/deez-dots.git
```

With `uv` as a one-off run:

```bash
uv tool run --from git+https://github.com/HyDE-Project/deez-dots.git deez --version
```

Then run it as:

```bash
deez --version
deez dots --config ./example/dots.toml --deploy
```

## PEX Release

You can build a single-file executable using `pex`:

```bash
python3 -m pip install pex
python3 -m pex . -e deez_dots:run_entrypoint -o target/deez.pex
```

Run the generated release file directly:

```bash
./target/deez.pex --version
```

## Man Page

Generate the complete single-page manual with:

```bash
make man
```

This uses `scdoc` to create `build/deez.1` in a single Arch-style manpage format.

Typical usage:

```bash
deez --version
./deez --help
./deez dots --help
./deez deps --help
```

