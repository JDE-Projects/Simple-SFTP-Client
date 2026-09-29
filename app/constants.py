GITHUB_REPO = "JDE-Projects/Simple-SFTP-Client"   # owner/repo for update checks
WORKER_COUNT = 2   # transfer queue workers by default, each its own SFTP session
# Ceiling for a batch of many small files. Every worker opens its own SFTP
# channel on the one SSH connection, alongside the browsing channel (which the
# folder watcher shares) and any Compare, Sync, or folder-scan channel.
# OpenSSH servers allow 10 sessions per connection by default (MaxSessions),
# so 5 leaves headroom under that common limit.
WORKER_COUNT_MAX = 5
# A background scan pauses queuing new files once this many are still WAITING,
# and resumes once the worker pool has drained enough of them. This is what
# keeps memory bounded while a 200GB / 1M-file folder is being scanned: the
# queue never grows past roughly this many items ahead of what the workers
# can drain.
SCAN_QUEUE_HIGH_WATER = 3000
# Two files count as the "same" for compare/sync only when their sizes and
# modification times both match. MTIME_TOL is the slack allowed on the time
# match (seconds): it absorbs whole-second rounding over the wire and the
# 2-second timestamp granularity of FAT/exFAT volumes, while staying tight
# enough that a real edit is never mistaken for unchanged.
MTIME_TOL = 2
# Compare and Sync build an in-memory map of every file and empty-folder
# marker on each side before they can classify anything, unlike the ordinary
# transfer scan which streams. COMPARE_SYNC_ENTRY_LIMIT caps entries added to
# each side's map, so a folder too large to hold in memory is refused with a
# clear message instead of running out of memory partway through.
COMPARE_SYNC_ENTRY_LIMIT = 500_000


# Weak / deprecated / CVE-prone algorithms we refuse (secure-or-fail).
DISABLED_ALGORITHMS = {
    "kex": ["diffie-hellman-group1-sha1", "diffie-hellman-group14-sha1",
            "diffie-hellman-group-exchange-sha1"],
    "ciphers": ["3des-cbc", "aes128-cbc", "aes192-cbc", "aes256-cbc",
                "blowfish-cbc", "cast128-cbc", "arcfour", "arcfour128", "arcfour256"],
    "macs": ["hmac-md5", "hmac-md5-96", "hmac-sha1-96", "hmac-sha1"],
    "keys": ["ssh-dss"],
}
