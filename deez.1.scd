NAME(1) "deez v0.1.0" "User Commands"
# NAME
deez - dot bundle manager

# SYNOPSIS
deez [*--debug*] [*--version*] command ...

# DESCRIPTION
deez is a command-line tool for packaging, installing, exporting, and restoring dot bundles.
It supports dotfile deployment, dependency management, backup snapshots, and cache inspection.

# OPTIONS
- *-h*, *--help*
  show this help message and exit.
- *--debug*
  enable debug logging for troubleshooting.
- *--version*
  print the current version and exit.

# COMMANDS
- *dots*
  Dotfile deployment operations.

## dots
- *-c*, *--config* _CONFIG_
  path to the dots TOML configuration file.
- *--package* _DOT_ ...
  pull from git source and bundle dots into tar.gz artifacts (no live changes); omit names for interactive selection or specify dot names or *all*.
- *--export* _DOT_ ...
  snapshot live dots from $HOME into tar.gz bundles; omit names for interactive selection or specify dot names or *all*.
- *--install* _TARBALL_ ...
  install from one or more bundle tar.gz files.
- *--deploy* _DOT_ ...
  bundle then install in one step (equivalent to *--package* + *--install*); omit names for interactive selection or specify dot names or *all*.
- *--uninstall* _DOT_ ...
  uninstall dots; omit names for interactive selector.
- *--restore* _DOT_ ...
  restore files from a backup snapshot; omit names for interactive selector.
- *--downgrade* _DOT_ ...
  re-install a previously cached bundle version; omit names for interactive selector.
- *--list*
  list all tracked dots and their state.
- *--no-backup*
  skip backup when deploying or uninstalling.
- *--no-deps-checks*
  skip dependency checks before install or deploy.
- *--no-deps-install*
  check dependencies but do not auto-install missing ones before install or deploy.
- *--no-compress*
  skip tar.gz packing; leave output as a plain directory for inspection.
- *--force*
  remove an existing extracted build directory beside the output tarball before writing.
- *--dry-run*
  show what would happen without making live changes.

- *deps*
  Dependency operations.

## deps
- *-c*, *--config* _CONFIG_
  path to the dots TOML configuration file.
- *--check*
  check dependency status.
- *--install*
  install missing dependencies.
- *--update*
  update packages via configured package managers.
- *--manager* _MANAGER_
  limit to specific manager(s), e.g. *--manager* yay.

- *backup*
  Backup operations.

## backup
- *--list*
  list backup snapshots.
- *--prune* _DOT_ ...
  prune old backups; optionally limit pruning to one or more dots.
- *--keep* _NUMBER_
  number of newest snapshots to keep when pruning (default: 5).
- *--dry-run*
  show what would be deleted without removing files.

- *cache*
  Cache operations.

## cache
- *--list*
  list cached bundle archives.
- *--prune*
  prune old cached bundle archives.
- *--keep* _NUMBER_
  number of newest cache entries to keep when pruning (default: 10).
- *--dry-run*
  show what would be deleted without removing files.

# EXAMPLES
- *deez dots --config ./example/dots.toml --deploy*
  bundle and deploy the configured dots in one step.
- *deez deps --config ./example/dots.toml --check*
  check dependency status for configured dots.
- *deez cache --list*
  list cached bundle archives.
- *deez backup --prune --dry-run*
  show which backup snapshots would be pruned without deleting them.

# SEE ALSO
- *man* (1)
