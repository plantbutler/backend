# plantbutler / backend

The Plant Butler backend: one Python container with SQLite on a bind-mounted volume, running in
Docker on the Synology NAS, LAN-only. It stores the raw readings the board reports, knows which
channel is which valve, pot and plant, hands one water command at a time to the board, and — later —
decides when to water and says when something is wrong.

Not started. What it will do and in which order is in the [plan](https://github.com/plantbutler/plan);
the decisions it is built on are in the [umbrella](https://github.com/plantbutler/plantbutler/blob/main/DECISIONS.md).
