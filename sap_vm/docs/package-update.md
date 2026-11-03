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

`dnf update` runs polled via `async` (`guest_dnf_update_async`, default
3600s; `guest_dnf_update_poll`, default 15s) since how long it takes depends
entirely on how far `compose_url` has drifted from
`guest_image_compose_url`. Set `-e guest_update_packages=false` to leave the
guest on the packages it was installed with.
