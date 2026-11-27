# Package update (after first boot)

`repo_update.yml` runs right after `post_boot.yml`, gated by
`guest_update_packages` (default `true`):

1. Rewrites the guest's yum repo files (`ansible.builtin.yum_repository`,
   same repo `name`s as `vm_repos` so this overwrites them in place) to
   point at `compose_url` instead of `guest_image_compose_url`
   (`vm_update_repos`).
2. Runs `dnf update` (`name: '*', state: latest, update_cache: true`) on the
   guest so it picks up whatever newer packages/errata `compose_url`
   carries — without re-downloading a guest image or reinstalling from
   scratch.
3. Activates a new guest kernel when `dnf update` installed one:
   - Re-applies latency cmdline (`transparent_hugepage=`, `mitigations=off`,
     C1 C-states, `cpuidle_haltpoll.force=1`) to **all** grubby entries (a
     newly installed kernel would otherwise miss the earlier
     `post_boot.yml` pass).
   - Sets `grubby --set-default` to the newest `kernel-core` RPM if it is
     not already the default boot entry.
   - Reboots the guest when `uname -r` is not that newest kernel
     (`vm_guest_reboot_timeout`), then verifies `uname -r`.

`dnf update` runs polled via `async` (`guest_dnf_update_async`, default
3600s; `guest_dnf_update_poll`, default 15s) since how long it takes depends
entirely on how far `compose_url` has drifted from
`guest_image_compose_url`. Set `-e guest_update_packages=false` to leave the
guest on the packages it was installed with.

Without step 3, a newer kernel can be installed under `/boot` while the
guest keeps running the image's original kernel and/or boots the wrong
default entry on the next manual reboot.
