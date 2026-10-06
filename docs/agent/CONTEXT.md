# cimkit-git

cimkit-git makes ArcGIS `.aprx` projects version-controllable, and — when a project opts in — swaps environment-specific data connections in and out as the project moves between environments.

## Language

### Project & source

**Project**:
An ArcGIS Pro project — the thing a user opens and edits. Ships as a single `.aprx` binary.
_Avoid_: map, document, file

**Source**:
The exploded, human-diffable rendering of a project that is committed and merged in place of the binary. The canonical source of truth; the binary is a regenerated artifact.
_Avoid_: extracted dir, working copy

**Explode**:
Turn a project binary into its source.

**Pack**:
Turn source back into a project binary.

### Mode

**Mode**:
Which of the tool's two purposes a project has opted into. A project is in exactly one mode, declared once and shared by the whole team.
_Avoid_: profile, flavour

**Simple mode**:
Version control only — source is a faithful, environment-independent rendering of the binary, with nothing swapped.

**Environment mode**:
Version control plus connection substitution — source is neutral (see _Neutral source_) and the binary is rebuilt per environment.

### Connections & environments

**Connection string**:
The data-source locator embedded in a project (a geodatabase or SDE connection) whose value differs from one environment to the next.
_Avoid_: data source, workspace path

**Environment**:
A named deployment target (e.g. `dev`, `uat`, `prd`) that supplies the real connection strings for that target.
_Avoid_: stage, profile

**Token**:
The environment-neutral placeholder that stands in for a connection string in committed source.

**Neutral source**:
Source in which every connection string has been replaced by a token — safe to commit and merge across all environments.

**Tokenize**:
Replace real connection strings with tokens while exploding, producing neutral source.

**Substitute**:
Replace tokens with a chosen environment's real connection strings while packing.
