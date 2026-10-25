# Seeing what the Kobo does: Nickel's debug log

Nickel, the Kobo's reading app, can stream a detailed log over Wi-Fi. It shows each step of a
Sync and why a book is or isn't downloaded. This is only needed for troubleshooting.

**Be careful:** unlike the rest of the setup, which only edits a settings file over USB, this
changes system settings on the Kobo. Follow the steps exactly and keep the backups.

## 1. Turn on developer options

Type `devmodeon` in the Kobo's search field and search. There is no confirmation message; it
worked if **Settings → Device information** now ends with **Developer options**.

## 2. Turn on SSH

Connect the Kobo with USB, and in the hidden `.kobo` folder rename `ssh-disabled` to
`ssh-enabled`. Eject, and let the Kobo reconnect to Wi-Fi.

From a computer on the same network:

```sh
ssh root@<kobo-ip>
```

The first login asks you to set a password. This is the Kobo's own local system account, **not**
your Kobo account; pick a new one and don't reuse your Kobo password.

## 3. Turn on debug logging

Qt, the toolkit Nickel is built with, reads its logging rules from a small file. Over SSH:

```sh
# Where Nickel looks for its settings: XDG_CONFIG_HOME if set, otherwise HOME/.config
tr '\0' '\n' < /proc/$(pidof nickel)/environ | grep -E '^(HOME|XDG_CONFIG_HOME)='

# With HOME=/root (use the folder you found):
mkdir -p /root/.config/QtProject
printf '[Rules]\n*.debug=true\n' > /root/.config/QtProject/qtlogging.ini
cat /root/.config/QtProject/qtlogging.ini     # shows the two lines
reboot
```

If no debug lines show up in step 4, there is a fallback: add `QT_LOGGING_RULES="*.debug=true"`
to the line that starts Nickel in `/etc/init.d/rcS`. **Make a copy first**
(`cp /etc/init.d/rcS /etc/init.d/rcS.orig`): a mistake in that file can keep the reading app from
starting until it is fixed over SSH.

## 4. Capture a Sync

On the computer, start listening **before** pressing Sync on the Kobo:

```sh
nc <kobo-ip> 5001 | tee kobo-debug.log
```

Press Sync, wait until all downloads are done, and stop with Ctrl+C.

Useful lines:

| Line | Meaning |
| --- | --- |
| `LibraryNextPageCommand::downloadBooks(...) "<title>"` | one line per book the Kobo decided to download after this Sync |
| `Saved volume "<title>" to ...` | a download finished |
| `Removing volume "<title>"` | a book left the Kobo (read elsewhere, or out of the window) |
| `packetdump.warning` | a request that failed, with the full URL |

```sh
grep -nE 'downloadBooks|Saved volume|Removing volume|packetdump.warning' kobo-debug.log
```

## 5. Before sharing a log

The log contains your Neneko token and host name (in every URL) and your Kobo account ID. Remove
them first:

```sh
sed -E 's#/kobo/[A-Za-z0-9_-]+#/kobo/<token>#g; s#https?://[^/" ]+#http://<host>#g' kobo-debug.log \
  | grep -v 'Authenticated user' > kobo-debug-clean.log
```

Then look through `kobo-debug-clean.log` once more for anything personal.

## 6. Turning it off again

```sh
ssh root@<kobo-ip>
rm /root/.config/QtProject/qtlogging.ini            # or: cp /etc/init.d/rcS.orig /etc/init.d/rcS
exit
```

Over USB, rename `.kobo/ssh-enabled` back to `ssh-disabled`. Type `devmodeoff` in the search
field, then restart the Kobo.

## Searching the reader software

Nickel's library contains readable names of its functions and settings, which can hint at how it
behaves. It contains nothing personal:

```sh
ssh root@<kobo-ip> 'cp /usr/local/Kobo/libnickel.so.1.0.0 /mnt/onboard/ && sync'
# connect USB, copy libnickel.so.1.0.0 from the Kobo drive, then delete it there
strings libnickel.so.1.0.0 | grep -i download | sort -u
```

The command names and the port-5001 log come from the Kobo protocol notes
(`docs/kobo-api.md`) of the books project, a self-hosted library server with Kobo sync.
