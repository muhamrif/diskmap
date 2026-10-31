# diskmap

A one-stop live dashboard for your Mac: everything about it, plus one-click fixes.

Nothing is quit, deleted or changed until you click (and confirm) it.

## Install on your Mac

Open **Terminal** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/diskmap/main/install.sh | bash
```

It downloads the code, builds the app on your Mac (about a minute) and opens it from `~/Applications`.
If it says you need Apple's Command Line Tools, click **Install** in the window that pops up, wait for it to finish,
then paste the command again. Nothing else gets installed.

Because the app is built on your own Mac, macOS opens it without the "unidentified developer" warning.

- **Update:** run the same command again.
- **First run:** in System Settings › Privacy & Security, give diskmap **Full Disk Access** (for the full disk map),
  click **Allow** when it asks to control Safari/Chrome/Finder/System Events, and allow its **Notifications**.
  The **Diagnostics** tab checks everything and has Fix buttons.
- **Works best on Apple silicon.** On Intel Macs a few sensors (GPU, some temperatures and power readings) show "n/a".
- **Private:** everything runs and stays on your Mac. Nothing is sent anywhere.
- **Uninstall:** quit diskmap, then delete `~/Applications/diskmap.app`, `~/.diskmap`,
  `~/Library/Application Support/diskmap` and, if you turned on Open at Login, `~/Library/LaunchAgents/local.diskmap.app.plist`.

## Run from source (no app)

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/diskmap && cd diskmap
python3 server.py          # opens http://127.0.0.1:7777 — Ctrl-C to stop
```

Standard library only (Python 3.8+), no pip installs.

## Mac app

```bash
bash ~/projects/diskmap/macapp/build.sh     # builds and installs ~/Applications/diskmap.app, then opens it
```

- Native window plus a menu bar item with live CPU and temperature, alerts, Rescan Disk and Open at Login.
- Closing the window keeps diskmap running in the menu bar so history keeps recording. ⌘Q quits it fully.
- ⌘1 City, ⌘2 Today, ⌘3 Live … ⌘9 History; ⌘] / ⌘[ next / previous city; ⌘R reloads, ⇧⌘R restarts the dashboard, ⌘+ / ⌘− zoom.
- Data lives in `~/Library/Application Support/diskmap` (app menu → Show Data Folder).
- Re-run `build.sh` after changing any dashboard file; the app bundles its own copy.
- Grant **Full Disk Access** to diskmap.app (not Terminal) for the complete disk map. After a rebuild macOS may ask again.
- Native notifications with action buttons (Quit the runaway app, open Storage…) for heat, low disk/battery, memory pressure,
  heavy swapping, runaway apps, crashes and finished jobs. Tune them in Settings.
- **⌘K** opens the command bar: quit apps, stop ports, unload models, run tasks, open any tab or System Settings pane.
- Click any app (Overview or History) for its own CPU / memory / energy history.
- **Diagnostics** tab checks every sensor, permission and tool, with fix buttons and a "Copy report" button.
- **Autopilot** (Settings, off by default): weekly safe cleanup, unload AI models or close duplicate tabs under memory pressure.
- When the window is closed it samples less often (every 10 s by default), and every 4 s on battery.

## Tabs

| Tab | What's there |
|---|---|
| **Live** | CPU, GPU, memory, disk, temperature, power, network, disk activity (live); health cards (security score, updates, repos, crashes, battery, cleanable space); every suggested action from every tab; apps by memory / CPU / energy with Quit |
| **Processes** | Every process with search, sort, Stop / Kill, full command line; browser tabs (close duplicates / heavy / single); what's keeping the Mac awake |
| **Hardware** | Per-core load, power by component, fans, all temperature sensors, battery (health, cycles, watts), Bluetooth & USB devices, system & memory paging, full System Information inventory (serials masked) |
| **Network** | Wi-Fi details, interfaces & IPs, router/internet ping, apps using the network, open connections per app, DNS, VPN, optional public IP, listening ports, sharing services |
| **Storage** | Read/write & IOPS, volumes, SMART, Time Machine, biggest files, likely duplicates, the disk treemap and cleanup (Clean now or copy a script) |
| **Software** | Every app with size, version, last opened, Intel-only flag (Trash / Show); macOS updates; Homebrew outdated + one-click upgrade/cleanup; login items & launch agents (flags leftovers from uninstalled apps) |
| **Security** | FileVault, SIP, Gatekeeper, firewall, auto-updates, screen lock, XProtect, sharing, Find My — with a score and buttons to the right Settings pane; every open port |
| **Developer** | Git repos with uncommitted/unpushed work, toolchain versions, Docker disk use + prune, Ollama models, maintenance tasks with live output |
| **History** | CPU, memory, temperature, power, GPU, network, disk I/O, battery, free space, swap, ping over 1 h – 30 days; busiest apps; alerts & cleanups |
| **Logs** | Crash / hang / panic reports, sleep & wake history, restarts |
| **Settings** | macOS notifications (hot CPU, low disk, low battery, memory pressure), public IP lookup, history retention |

History and settings live in `data/` next to the app. Leave the server running to build up history.

## City (the main page)
A full-window live skyline where every landmark is one of your Mac's stats. Pick a city from the button at the top right
(or View › City in the menu bar, or press `[` / `]`): **New York, Dubai, Karachi, Paris, London, Tokyo**.

| Stat | New York | Dubai | Karachi | Paris | London | Tokyo |
|---|---|---|---|---|---|---|
| CPU (floors light up) | Empire State | Burj Khalifa | Bahria Icon Tower | Eiffel Tower (sparkles above 70%) | The Shard | Skytree |
| Memory (glass fills) | One WTC | Burj Al Arab | Habib Bank Plaza | Tour Montparnasse | The Gherkin | Cocoon Tower |
| Free disk (lit lights) | Chrysler crown | Dubai Frame | MCB Tower | Arc de Triomphe | Big Ben's dial | Tokyo Tower |
| GPU | Searchlights | Museum of the Future | Mazar-e-Quaid floodlights | Eiffel beacon | London Eye | Neon signs |
| Temperature | Liberty's torch | Emirates Towers | Teen Talwar | Sacré-Cœur | St Paul's | Mt Fuji turns red |
| Network (headlights ↓ / taillights ↑) | Brooklyn Bridge | Sheikh Zayed Road | Native Jetty Bridge | Pont Alexandre III | Tower Bridge | Rainbow Bridge |
| Battery | Water tower | Wind tower | Clock tower | Moulin Rouge | Phone box | Vending machine |
| Fans | Manhole steam | Dubai Fountain | Chai steam | Trocadéro fountains | Battersea smoke | Ramen steam |

Plus: trains underground = disk reads/writes, vehicles (cabs, rickshaws, buses, bateaux-mouches) = open apps,
the big screen = your to-dos. Anything in trouble gets a pulsing ring.

- **Hover** a landmark for its value; **click** it (or a tile in the dock) for the inspector: history, details, top apps and fix buttons.
- **Sky**: Auto (your clock), City time (that city's clock), or pin dawn / day / dusk / night. Tonight's real moon phase.
- **Labels**: Values / Names / Off (`L`). Choices are saved.

## Today
The rundown: needs attention, focus modes, memory growth, storage, this week, battery, software & safety, right now.

## Also
- **Focus modes** (Home, menu bar): quit a set of apps, unload AI models, close heavy tabs; ending it reopens them. Optional Shortcuts to run on start/end (e.g. one that turns on a macOS Focus).
- **Memory growth**: flags apps whose memory keeps climbing since launch, with a Restart button and a notification.
- **Battery report**: health over time, cycles per week, typical drain and runtime, what drains it on battery.
- **Weekly digest**: Sunday evening notification + "This week" card.
- **Large-file watcher**: notices sudden drops in free space and finds the file or folder that grew.
- **Desktop widget** (menu bar › Desktop Widget): small / medium / large, sits on your desktop; drag to move, double-click to open diskmap.
- **Light / Auto / Dark** in the header or View › Appearance.

## Permissions macOS may ask for
- **Automation** → Chrome/Safari (tabs), Finder (move to Trash), System Events (login items). Click Allow.
- **Full Disk Access** for your terminal app gives a complete disk map and all logs.
- **Notifications** for your terminal app, for alerts.

## Options
```
python3 server.py --port 8080         # different port
python3 server.py --interval 5        # sample less often
python3 server.py --rescan-min 0      # only rescan the disk when you click
python3 server.py --lan               # view from phone / Mac mini on your Wi-Fi (read-only there)
python3 macstats.py                   # print every hardware sensor reading (self-test)
python3 inventory.py security apps    # print any inventory section
python3 diskmap.py                    # one-off static disk report, no server
```

## Safety
Listens only on 127.0.0.1 (unless `--lan`), checks the Host header, and every action needs a per-run token only
this Mac's page gets. The server re-checks every action: it only quits running non-system apps, stops your own
processes, closes tabs it can see, moves to the Trash only files and apps it listed (via Finder, so you can put them
back), removes only your own launch agents, and runs only its own whitelisted maintenance commands.
# diskmap
# diskmap
