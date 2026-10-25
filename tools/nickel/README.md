# Reading libnickel

`nickel.py` is a small disassembly helper for Kobo's reading app library. Neneko's notes on how
the Kobo syncs and picks downloads ([`docs/kobo-notes.md`](../../docs/kobo-notes.md), "How Nickel
syncs and downloads") come from it, so the findings can be checked again after a firmware update.

It contains no Kobo code and no output from Kobo's library. You run it on your own copy, taken
from your own Kobo, to understand the protocol Neneko has to speak (interoperability). Don't
commit the library, a strings dump of it, or disassembly listings to this repository.

## Setup

```sh
python3 -m venv ~/.venvs/nickel
~/.venvs/nickel/bin/pip install -r tools/nickel/requirements.txt
```

It also needs `c++filt` (part of binutils). Copy the library off the Kobo over USB or SSH
(`/usr/local/Kobo/libnickel.so.1.0.0`) into a folder of its own outside the repository. It is
about 24 MB, a 32-bit ARM shared object with its C++ names exported.

## Use

```sh
N="$HOME/.venvs/nickel/bin/python -I tools/nickel/nickel.py /path/to/libnickel.so.1.0.0"
$N dis 'FeatureSettings::maxBooksToDownload'       # the code, with calls, strings and GOT entries named
$N dis 'LibraryParser::parseHeaders' --calls       # only calls and references
$N callers 'ResolveEntitlementUpdatesCommand::downloadRecentBooks'
$N strref x-kobo-recent-reads                      # code that uses a string
```

`dis` matches every function whose name contains the text, or takes an address (`0x88ac04`).
The first run builds a symbol table (a few seconds) and caches it in
`~/.cache/neneko-nickel` (`--cache DIR`, `--no-cache`).

Reading the output:

- The code is Thumb-2. Arguments go in `r0`–`r3` (`r0` is `this` for methods), then on the stack.
  Constants such as defaults show up as `movs r1, #5` just before a call, so leave out `--calls`
  to see them.
- `-> "text"` is a string the code loads. `-> GOT name` is an imported symbol. Loads like
  `ldr r3, [r5, r3]` are GOT offsets the helper doesn't resolve; add the literal to the GOT base
  shown at the start of the function.
- Code that isn't exported (lambdas, static initialisers, template helpers) is labelled with the
  nearest exported name before it, prefixed with `~` for jump targets. Treat those names as
  locations, not as what the code is.
- Qt signals are connected with `QObject::connectImpl`. The signal and slot are the two GOT
  entries loaded just before the call.

## Checking the sync findings after a firmware update

| Finding | Command | Look for |
| --- | --- | --- |
| Download cap and its default | `dis 'FeatureSettings::maxBooksToDownload'` | `"MaxDownload"`, `movs r1, #5` before `QVariant(int)` |
| Header names Nickel reads | `dis 'LibraryParser::parseHeaders' --calls` and `strref x-kobo-recent-reads` | four `hasRawHeader` calls, `numRecentBooksFirst` after `toInt` |
| Per-page downloads | `dis 'ResolveEntitlementUpdatesCommand::downloadRecentBooks'` | `maxBooksToDownload`, compared with the header's value (`[r?, #0x24]`) |
| The cap per call | `dis 'LibraryNextPageCommand::downloadBooks'` | loop that appends while the count is below `maxBooksToDownload` |
| After the last page | `dis 'LibraryNextPageCommand::lastPageParsed' --calls` | `retrieveSyncedBooks`, then `getBooks` |
| Known books aren't downloaded again | `dis 'LibraryNextPageCommand::canDownload'`, `dis 'VolumeManager::shouldRedownload'` | `ldrb ... #0x188` (set in `handleBook` from `shouldRedownload`); `getById`, then `isFullBookAccessible` / `hasDownloadUrl` compared |
| One sync time per sync | `dis 'LibraryNextPageCommand::initializeSync' --calls`, `dis 'ResolveEntitlementUpdatesCommand::handleBook' --calls` | `getSyncTime` → `setLibrarySyncTime`, and `setSyncTime` on each book |
| Library sorting | `dis 'RecentKey<Volume>::key'`, `dis 'ReadingState::updateVolume' --calls`, `dis 'Volume::getDateAddedSortKey' --calls` | `DateLastRead` at `[d, #0x28]`; `setDateLastRead` from `priorityTimestamp` (`[r5, #0xc]`), which `ReadingStateParser::parseReadingState` raises to the latest `…LastModified`; `EntitlementManager::getCreatedRaw` |
| Where the setting lives | `dis 'FeatureSettings::sectionName'` | an address holding a length (15) and a pointer to the text `FeatureSettings` |

The notes were made from firmware 4.38.23828.
