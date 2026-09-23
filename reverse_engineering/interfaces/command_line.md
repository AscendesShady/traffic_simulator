# Command-line and configuration interface

## Purpose

All major SUMO executables share a typed option registry and a configuration-
file overlay mechanism. Each executable registers its own option families but
uses `OptionsIO`, `OptionsParser`, `OptionsLoader`, `OptionsCont`, and
`SystemFrame` for parsing, validation, help, configuration serialization, and
reporting.

Primary implementation:

- `src/utils/options/OptionsCont.*`
- `src/utils/options/OptionsIO.*`
- `src/utils/options/OptionsParser.*`
- `src/utils/options/OptionsLoader.*`
- `src/utils/common/SystemFrame.*`
- executable `*_main.cpp` files and subsystem `*Frame.cpp` files

Related tests: each tool's `meta/help`, `meta/version`, `meta/write_config`, and
`errors/unknown_option` cases; option utility tests are indirectly functional.

## Precedence and loading

`OptionsIO::getOptions()` first preparses command-line arguments so it can find
a configuration file, loads that XML through Xerces/`OptionsLoader`, then parses
the command line again. Therefore explicitly supplied command-line values
override configuration values. If a single positional XML file is given,
`OptionsIO::getRoot()` may map its root element to a registered file option.

File values from configurations are relocated relative to the configuration
file by `OptionsCont::relocateFiles()`. Environment substitution occurs in
`OptionsCont::set()` using the option-load timestamp context.

## Typed registry

Executables call `doRegister()` with `Option_*` instances, then attach short
names, synonyms, XML defaults, subtopics, descriptions, and constraints.
Consumers retrieve typed values (`getBool`, `getInt`, `getFloat`, `getString`,
vectors). Unknown names and invalid conversions are errors.

Meta options implemented by `OptionsCont::processMetaOptions()` include help,
version, configuration/template/schema writing, and other common early exits.

## Contract for adding an option

1. Register it before `OptionsIO::getOptions()`.
2. Add its description/subtopic and any compatible synonym.
3. Validate cross-option constraints in the owning frame's `checkOptions()`.
4. Consume the typed value after parsing.
5. Add configuration and CLI tests, including invalid values and precedence.

Renaming an option can break CLI scripts, configuration XML, GUI launchers,
tests, and Python tools. Preserve aliases or document migration.

## Edge cases

- Multiple configuration files/ambiguous positional XML roots.
- Relative paths interpreted against the configuration location rather than
  process working directory.
- Deprecated synonyms and duplicate settings.
- Meta options that intentionally exit before building a network.
- Locale-sensitive messages normalized by the test harness.

## Confidence

High — parsing order is explicit in `OptionsIO.cpp:75-140` and registry behavior
in `OptionsCont.cpp`.
