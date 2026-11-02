# RHEL compose (image, install repos, and update repos)

Two composes are involved, and they are intentionally **different variables**:

| Variable | Role | Must be a base/GA compose? |
|---|---|---|
| `guest_image_compose_url` | Sources the qcow2 **and** the guest's first-boot (cloud-init) yum repos (`vm_repos`) | **Yes** — needs `BaseOS/x86_64/images/` |
| `compose_url` | Sources the repos the guest is repointed at *after* boot (`vm_update_repos`, `tasks/repo_update.yml`), then `dnf update` runs against it | No — repo-only composes (e.g. `-updates-*`) are fine |

Both must point at the directory that directly contains the Variant
directories (`BaseOS`, `AppStream`, `CRB`, `SAP`, `SAPHANA`); different labs
put that at different depths, so both include whatever prefix your lab needs:

| Lab | `guest_image_compose_url` (default) | `compose_url` (default) |
|---|---|---|
| TLV | `http://download.eng.tlv.redhat.com/rhel-10/composes/RHEL-10/RHEL-10.2-20260507.1/compose` | `http://download.eng.tlv.redhat.com/rhel-10/composes/RHEL-10/RHEL-10.2-updates-20261001.1/compose` |
| Scale lab | `http://mirror.scalelab.redhat.com/RHEL10/10.2` (no `compose/` subdir) | same |

> **Why two composes:** RHEL's `-updates-*` composes refresh package
> repodata but do **not** rebuild `BaseOS/x86_64/images/`. Pointing
> `guest_image_compose_url` (or the old, now-removed single `compose_url`)
> at a `-updates-*` build 404s on the qcow2:
> `rhel-guest-image-10.2-updates-<date>....qcow2` does not exist. So the
> guest is always **installed** from the last GA/base compose
> (`guest_image_compose_url`), then **updated** in place from whatever
> newer compose `compose_url` points at (`repo_update.yml` + `dnf update`,
> see [Package update](package-update.md)) — no need to rebuild/re-download a guest image just to pick up
> newer packages.

`compose_release` (default `10.2`, used for repo names/descriptions) and
`guest_image_compose_id` (default `10.2-20260507.1`, the exact dated build id
embedded in the qcow2 filename) are **independent** of both compose URLs — a
mirror's URL does not necessarily encode either the same way a compose's
does, and a lab like Scale lab mirrors a moving "latest" build under a
release-only path (`.../10.2/`) with no date in the URL at all. Keep
`guest_image_compose_id` paired with `guest_image_compose_url` (a GA/base
build), never with `compose_url`. Override together when switching
labs/builds:

```bash
-e guest_image_compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
-e compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
-e compose_release='10.2' \
-e guest_image_compose_id='10.2-20260408.1'
```

| Derived value | Rule |
|---|---|
| Remote qcow2 | `{guest_image_compose_url}/BaseOS/x86_64/images/rhel-guest-image-{guest_image_compose_id}.x86_64.qcow2` |
| Local copy | `guest_image_path`, default `/home/kvm/rhel10-2-base.qcow2` (`guest_image_filename`) |
| Install-time repos (`vm_repos`) | BaseOS, AppStream, CRB, SAP, SAPHANA under `{guest_image_compose_url}/<Name>/x86_64/os/`, each `skip_if_unavailable` so a lab missing SAP/SAPHANA just skips those two |
| Update-time repos (`vm_update_repos`) | Same Variants under `{compose_url}/<Name>/x86_64/os/`, same repo names as `vm_repos` so `repo_update.yml` overwrites them in place |

Only the **local** image file name is independent of the lab. Download
timeout is `guest_image_download_timeout` (default 3600s); a 1 GiB qcow2
exceeds Ansible `get_url`'s 10s default.
