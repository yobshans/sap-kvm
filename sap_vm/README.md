# sap_vm

Ansible role that creates a KVM guest for SAP HANA / HCMT benchmarking. On each
run it reads the hypervisor (CPU, NUMA, memory, disk, TSC) and builds a libvirt
domain from that hardware, following SAP HANA on KVM / RHV sizing rules.

The wrapper playbook is `create_sap_vm.yml` at the repository root. It targets
the **`hypervisor`** inventory group (this machine or a remote hypervisor).

**Guest OS** is RHEL 10.2. The **hypervisor** can be RHEL 9.6 or 10.2.

## How VM size is calculated

Discovery reads sysfs with Ansible (`find`, `slurp`) and renders
`templates/hana_topology.j2`. Results are applied as facts and written to
`/home/kvm/<vm_name>-topology.json`.

Set a variable to `auto` (the default) to derive it. Pass a concrete extra-var
to override; pinning still comes from the live host topology.

### CPU and NUMA

| Guest setting | Rule |
|---|---|
| NUMA cells / sockets | One guest NUMA cell (and vSocket) per **host NUMA node** |
| Threads per core | Host SMT width (`thread_siblings`) |
| Cores per socket | Physical cores on that node **minus 1** |
| vCPUs | `sockets × cores × threads` |

The first physical core of each NUMA node is left for QEMU (`emulatorpin`) and
the IO thread (`iothreadpin`). Remaining cores are pinned **strictly 1:1**:
each guest vCPU gets exactly **one** host SMT thread, e.g.

```xml
<vcpupin vcpu='0' cpuset='2'/>
<vcpupin vcpu='1' cpuset='42'/>
```

not the whole sibling pair shared across both vCPUs of a core
(`cpuset='2,42'` for both) — letting the host scheduler bounce a vCPU
between its own two SMT siblings hurts cache locality and adds jitter for
CPU-pinned benchmarks (SAP PBO/HCMT). This is purely a libvirt
`<cputune>` change (`templates/hana_topology.j2`); it doesn't change vCPU
*count*.

Example: 2 sockets, 20 cores/socket, SMT-2 → reserve 1 core/node →
`2 × 19 × 2 = 76` vCPUs, 2 NUMA cells.

`vm_vcpus` must be divisible by `numa_nodes × threads`.

### Memory and hugepages

Guest memory is balanced across NUMA cells and aligned to **1 GiB** hugepages
on the **hypervisor**. The guest is not configured with `hugepagesz=1G`; HCMT
sees normal RAM that is already 1 GiB-backed by the host.

1. Read `MemTotal` per host NUMA node (falls back to host `MemTotal` if sysfs
   parse yields 0).
2. Reserve hypervisor RAM: default **100 GiB** total (`hypervisor_reserve_gib`),
   split across nodes, at least `hypervisor_reserve_min_gib_per_node` (2 GiB)
   per node.
3. On small hosts, if that reserve would leave less than one hugepage per
   node, the reserve is reduced (about 1/8 of node RAM, still ≥ 2 GiB when
   possible).
4. Per-node guest RAM = `min(node MemTotal) − per-node reserve`, rounded down
   to a whole number of hugepages.
5. Total guest RAM = per-node RAM × NUMA nodes, and not below
   `vm_memory_min_gib` (default **64 GiB**) so HCMT is not given a 1–2 GiB VM.

1 GiB pages usually cannot be allocated on a fragmented running hypervisor. If
runtime reservation fails, the role stops and prints `grubby` commands. Set
`nr_hugepages` on the **host** kernel command line, **reboot the hypervisor**,
then re-run.

`vm_memory_prefault` (default `true`) adds `<allocation mode='immediate'/>`
inside `<memoryBacking>` in the domain XML, forcing all guest memory to be
faulted in at VM boot instead of on first touch, so page-fault overhead
doesn't skew early benchmark numbers. Requires libvirt ≥ 6.0.0 / QEMU ≥ 4.0;
set `-e vm_memory_prefault=false` if an older libvirt rejects the
`<allocation>` element.

Inside the guest, the kernel command line includes
`transparent_hugepage=always` so transparent hugepages are enabled before
tuned starts. The live sysfs knobs
(`/sys/kernel/mm/transparent_hugepage/enabled` and
`/sys/kernel/mm/transparent_hugepage/defrag`) are also set to `always` and
persisted with `/etc/tmpfiles.d/sap-thp.conf`.

### Disk

OS disk default is 150 GiB (`vm_disk_preferred_gib`), or less if the filesystem
under `/home/kvm` is tight (`vm_disk_reserve_gib` left free). Override with
`vm_disk_size` (for example `150G`).

If a libvirt pool already owns `kvm_base_dir` (this host uses pool `kvm` on
`/home/kvm`), that pool is reused instead of creating `sap-kvm`.

#### Raw disk format (`guest_raw_image`)

The VM disk is qcow2 by default (same format as the downloaded RHEL guest
image). Set `-e guest_raw_image=true` to run the guest off a raw disk
instead — some SAP HANA storage guidance prefers raw to skip qcow2's own
I/O layer. `tasks/raw_convert.yml` runs right after disk prep:

1. If `vm_disk_raw_path` (`{{ kvm_base_dir }}/{{ vm_name }}.raw`) already
   exists, it's a no-op — idempotent on repeated runs.
2. Otherwise, if the VM is currently running, it's shut down
   (`community.libvirt.virt`, `state: shutdown` — i.e. `virsh shutdown`),
   waiting until `virsh domstate` reports `shut off`.
3. `qemu-img convert -f qcow2 -O raw {{ vm_disk_path }} {{ vm_disk_raw_path }}`.
4. `vm_define.yml` (right after) regenerates the domain XML with
   `<driver name='qemu' type='raw' .../>` and
   `<source file='{{ vm_disk_raw_path }}'/>`, redefines, and starts the VM —
   so there's no separate "virsh start" step in `raw_convert.yml` itself.

This works the same way whether `guest_raw_image=true` is set on a brand
new VM (disk.yml just created the qcow2; nothing is running yet, so step 2
is skipped) or flipped on for an already-running qcow2 VM (steps 2–4 apply).

By default the pre-conversion qcow2 is **kept** (`guest_raw_image_remove_qcow2:
false`) as a point-in-time fallback — it will *not* stay in sync with the
guest once it's running from the `.raw` file. Set
`-e guest_raw_image_remove_qcow2=true` to delete it after a successful
conversion and free the space; once deleted, `tasks/disk.yml` stops
re-copying a fresh qcow2 on later runs (it checks whether the raw disk
already exists first). `-e destroy_existing_vm=true` removes both the
`.qcow2` and `.raw` files regardless of which one is currently in use.

#### Secondary data disk vs. NVMe PCI passthrough (`vm_nvme`)

The OS disk above (`vm_disk_path`/`vm_disk_raw_path`, attached as `vda`)
always exists — it's what the guest boots from. `vm_nvme` only controls a
**second** disk, attached as `vdb`, meant for SAP HANA data/log:

| `vm_nvme` | Task file | Guest sees |
|---|---|---|
| `false` (default) | `tasks/data_disk.yml` | `vdb` backed by a blank local qcow2 file, `vm_data_disk_path` (default `{{ kvm_base_dir }}/{{ vm_name }}-data.qcow2`), sized `vm_data_disk_size` (defaults to `vm_disk_size`) |
| `true` | `tasks/nvme.yml` | A real NVMe device (`/dev/nvmeXn1` inside the guest), PCI-passed-through with `<hostdev>` — no file-backed disk at all |

**`vm_nvme=true` discovery/bind sequence** (idempotent — safe to re-run):

1. When `vm_nvme=true`, host tuning automatically adds the correct IOMMU
   kernel arguments (`intel_iommu=on iommu=pt` on Intel, or
   `amd_iommu=on iommu=pt` on AMD) and reboots the hypervisor once with the
   other host-tuning changes. No manual IOMMU setup or interactive prompt is
   required.
2. Discovery is **PCI-first, not `lsblk`-first**: `lspci -D -d ::0108`
   enumerates every NVMe-class PCI controller regardless of which driver
   currently owns it. This matters because `lsblk`/`/sys/class/nvme` only
   show controllers currently bound to the host's `nvme` driver — once a
   controller is already bound to `vfio-pci` (from a previous run of this
   role, or because it was bound by hand ahead of time following the
   manual procedure below), it has no `/dev/nvmeXn1` node at all and is
   invisible to `lsblk`. Relying on `lsblk` alone would then wrongly report
   "no NVMe device found" on every run *after* the first successful bind —
   `lspci` sees the controller either way, so it's reused as-is without
   re-checking anything.
3. For controllers **still owned by the `nvme` driver** (not yet bound),
   `lsblk -o NAME,MODEL,SIZE,TYPE,TRAN,MOUNTPOINT` finds every `TYPE=disk`,
   `TRAN=nvme` device, then splits them into:
   - **free** — no mountpoint and no `children` (no partitions, no LVM) —
     eligible for passthrough. For example, on a host where the OS lives
     entirely on `sda` and the NVMe has never been touched:
     ```
     NAME                               MODEL                                  SIZE TYPE TRAN   MOUNTPOINT
     sda                                PERC H740P Mini                      446.6G disk
     ├─sda1                                                                      1M part
     ├─sda2                                                                    512M part        /boot
     └─sda3                                                                  446.1G part
       ├─vg_f20--h07--000--r640-lv_root                                      438.1G lvm         /
       └─vg_f20--h07--000--r640-lv_swap                                          8G lvm         [SWAP]
     sdb                                PERC H740P Mini                        1.7T disk
     sdc                                PERC H740P Mini                        1.7T disk
     nvme0n1                            Dell Express Flash PM1725b 3.2TB SFF   2.9T disk nvme
     ```
     `nvme0n1` has no `MOUNTPOINT` and no child rows at all → **free**.
   - **in_use** — already backing the hypervisor itself (e.g. a root/swap
     volume group laid directly on the whole NVMe namespace with no
     partition table, if the OS install picked the NVMe instead of a SAS
     disk for `sda`'s role above). These are never touched; the role fails
     if that's *all* that was found, rather than silently detaching storage
     the hypervisor needs.
4. The final passthrough candidate list is **already-vfio-pci-bound
   controllers (step 2) + lsblk-eligible controllers (step 3)**. Fails if
   that combined list is empty (every NVMe controller found is either
   backing the hypervisor or there's no NVMe hardware at all).
5. Gets each candidate's vendor:device ID via `lspci -ns` (works regardless
   of current driver), then `modprobe vfio-pci` and — unless a controller
   is already on `vfio-pci` from step 2 — unbinds it from its current
   driver and binds it to `vfio-pci`:
   ```bash
   echo "$NVME_PCI" > /sys/bus/pci/devices/$NVME_PCI/driver/unbind
   echo "vfio-pci"  > /sys/bus/pci/devices/$NVME_PCI/driver_override
   echo "$NVME_PCI" > /sys/bus/pci/drivers/vfio-pci/bind
   ```
6. Persists the binding across a hypervisor reboot:
   `/etc/modprobe.d/vfio.conf` (`options vfio-pci ids=<vendor:device,...>`),
   `/etc/modules-load.d/vfio-pci.conf` (`vfio-pci`), then `dracut -f` (only
   when either file actually changed).
7. `vm-domain.xml.j2` adds one `<hostdev mode='subsystem' type='pci'
   managed='yes'>` per candidate PCI address; `managed='yes'` lets libvirt
   itself detach/reattach the device from `vfio-pci` around VM start/stop,
   so no extra "give it back" step is needed between runs.

Once attached, the guest sees the raw namespace and can partition/format it
directly, e.g.:

```bash
lsblk | grep nvme
parted /dev/nvme0n1 mklabel gpt
parted /dev/nvme0n1 mkpart hana-data xfs 0% 80%
parted /dev/nvme0n1 mkpart hana-log  xfs 80% 100%
mkfs.xfs /dev/nvme0n1p1 && mkfs.xfs /dev/nvme0n1p2
mkdir -p /hana/data /hana/log
mount /dev/nvme0n1p1 /hana/data
mount /dev/nvme0n1p2 /hana/log
```

Before creating a KVM guest with `vm_nvme=true`, the role resets the
hypervisor NVMe namespace configured by `vm_nvme_device` (default
`/dev/nvme0n1`). It unmounts any filesystems on that device, removes the
`/hana/data` and `/hana/log` entries from `/etc/fstab`, wipes the
partitions and device, and removes the SAP resource-limit, tmpfiles, and
sysctl configuration files left by a host-side SAP HANA / PBO run. This
cleanup is noninteractive and runs before PCI passthrough preparation, even
when `/hana/data` and `/hana/log` are no longer mounted.

Destroying the VM (`destroy_existing_vm=true`) removes the local data
qcow2 file if one was used, but **does not** unbind a passed-through NVMe
device from `vfio-pci` — it's physical hardware, not a file under
`kvm_base_dir`, and leaving it bound means recreating the VM right after
reuses it without re-binding. To hand it back to the hypervisor's `nvme`
driver manually:

```bash
echo "$NVME_PCI" > /sys/bus/pci/devices/$NVME_PCI/driver/unbind
echo "nvme"       > /sys/bus/pci/devices/$NVME_PCI/driver_override
echo "$NVME_PCI" > /sys/bus/pci/drivers/nvme/bind
rm -f /etc/modprobe.d/vfio.conf /etc/modules-load.d/vfio-pci.conf && dracut -f
```

### Clock and CPU flags

TSC frequency is discovered from the host (`tsc_freq_khz` or kernel
calibration). The domain has a single `<timer name='tsc'/>`. If the CPU has
no TSC scaling and the value is still out of range, libvirt's advertised host
frequency is applied and start is retried. Override with
`-e vm_tsc_frequency=...` only on hosts that support TSC scaling.

The domain uses host-passthrough, L3 cache emulation, and `rdtscp` / `invtsc` /
`x2apic`. Memory ballooning is disabled.

## Hypervisor host tuning for CPU-pinned benchmarking (SAP PBO/HCMT)

> ⚠️ **This section changes the HYPERVISOR, not just the guest, and can
> reboot it.** `tasks/host_tuning.yml` always runs (there is no opt-out
> flag) and, when the running kernel's cmdline doesn't already carry the
> values the current topology needs, it runs `grubby` and **reboots the
> hypervisor** — taking down **every other VM currently running on that
> host**, not just this one. It is idempotent (a re-run with an
> already-matching cmdline is a no-op, and it only reboots again if the
> topology changes enough to need different values — e.g. a different
> `vm_vcpus` override), but **do not run this role against a shared or
> production hypervisor without accounting for that.**

### CPU isolation + static hugepages + disabled CPU mitigations (`tasks/host_tuning.yml`)

After topology discovery (`vm_cpu_pins`, `hugepages_per_node`,
`hugepage_nodes`) but before `hugepages.yml`'s runtime reservation,
`templates/host_tuning.j2` computes the desired hypervisor kernel cmdline:

- `isolcpus=<range>` / `nohz_full=<range>` / `rcu_nocbs=<range>` — exactly
  the host CPUs `vm_cpu_pins` dedicates to guest vCPUs (the 1:1 pins
  above), collapsed into ranges. The reserved emulator/iothread core is
  deliberately **left out** of this range — it needs to keep receiving
  normal host scheduling/interrupts/timers, so it's never isolated.
- `default_hugepagesz=1G hugepagesz=1G hugepages=<N>` — a static,
  boot-time 1 GiB hugepage reservation sized from
  `hugepages_per_node × hugepage_nodes` (the same total `hugepages.yml`
  already computes/verifies at runtime), more reliable than runtime
  reservation on a host whose memory is already fragmented.
- `mitigations=off` — disables CPU vulnerability mitigations on the
  hypervisor for benchmark throughput.

Example (2 sockets × 40 cores × SMT-2, 1 reserved core/node at CPU 0/40):

```bash
grubby --update-kernel=ALL \
  --remove-args="isolcpus nohz_full rcu_nocbs hugepages default_hugepagesz hugepagesz mitigations" \
  --args="default_hugepagesz=1G hugepagesz=1G hugepages=280 isolcpus=1-39,41-79 nohz_full=1-39,41-79 rcu_nocbs=1-39,41-79 mitigations=off"
```

This is compared against `ansible_cmdline` (the currently **booted**
kernel's `/proc/cmdline`, not just what's configured for the next boot):
if everything already matches, nothing happens. Otherwise, `grubby` runs
(`--remove-args` first so repeated topology changes replace the old
values instead of accumulating duplicates), the hypervisor **reboots**
(`ansible.builtin.reboot`, waits for it to come back), facts are
re-gathered, and the cmdline is checked again — failing loudly if it still
doesn't match (wrong default boot entry, multiple installed kernels,
etc.) rather than silently continuing on a half-tuned host.

**Sets `mitigations=off`** — disabling CPU vulnerability mitigations
(Spectre/MDS/L1TF/etc.) on the hypervisor is intentional for benchmark
throughput, but it is security-relevant and host-wide. Do not run this role
against a shared or production hypervisor unless that exposure is acceptable.

### KVM kernel module tuning (`tasks/kvm_tuning.yml`)

Runs right after host tuning (so it applies to the freshly (re)loaded
module if a reboot just happened). These are global `kvm`/`kvm_intel`
kernel module parameters — like host tuning above, they affect **every**
VM on this hypervisor, not just this one:

| Parameter | Default | Why |
|---|---|---|
| `/sys/module/kvm/parameters/halt_poll_ns` | `vm_kvm_halt_poll_ns` (2000000) | vCPU busy-waits longer before halting, reducing wakeup latency |
| `/sys/module/kvm/parameters/lapic_timer_advance_ns` | `vm_kvm_lapic_timer_advance_ns` (1000) | Compensates for VMEXIT latency on lapic timer delivery |
| `/sys/module/kvm_intel/parameters/ple_gap` | `vm_kvm_ple_gap` (0) | Disables Pause-Loop Exiting — benchmark workloads with spinning locks (e.g. SAP PBO) cause unnecessary VMEXITs under PLE |
| `/sys/module/kvm_intel/parameters/enable_apicv` | `Y` | Enables APICv and posted interrupts on Intel hosts |

Applied live via sysfs (effective immediately, no reboot) and persisted to
`/etc/modprobe.d/kvm-tuning.conf` so the same values come back on the next
module reload/reboot. `ple_gap` is Intel-specific (Pause-Loop Exiting); on
an AMD host (no `kvm_intel` module) that one tunable is skipped — not
failed — with a note in the output. AMD's equivalent Pause Filter tuning
is not currently handled by this role.

## RHEL compose (image, install repos, and update repos)

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
> see below) — no need to rebuild/re-download a guest image just to pick up
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

## First boot (cloud-init)

A NoCloud cidata ISO is built with `community.libvirt.virt_volume`
`create_cidata_cdrom` (Rock Ridge names `user-data` / `meta-data`). Cloud-init
applies:

- Hostname / FQDN (`vm_hostname`.`vm_domain`, default
  `sap-kvm-vm.lab.eng.tlv2.redhat.com`)
- Root password SSH (`ssh_pwauth`, `00-sap.conf` before RHEL
  `50-redhat.conf`, plaintext `chpasswd`)
- Yum repos from `guest_image_compose_url` (`vm_repos`), packages from
  `vm_packages` (includes `libxcrypt-compat` for HCMT) plus
  `qemu-guest-agent`
- Filesystem grow on `/dev/vda`
- SELinux `permissive` (`setenforce 0` equivalent)

Password SSH is required; the role does not inject SSH keys. Recreate with
`-e destroy_existing_vm=true` when user-data changes, because cloud-init
user-data runs on first boot only. `libxcrypt-compat`, THP=never, and SELinux
permissive are also applied over SSH after boot so an existing guest still
gets them.

`post_boot.yml` waits up to `cloud_init_boot_timeout` (default 1800s) for
`/var/lib/cloud/instance/boot-finished` before continuing. cloud-init is
installing real packages from 5 real repos on a working network (see below),
so on a slow mirror this easily exceeds a couple of minutes; a repo that is
unreachable/misconfigured can also make it stall closer to the full timeout
per repo before `skip_if_unavailable` gives up on it.

## Package update (after first boot)

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

## Guest network / DHCP lease and outbound access

`preflight.yml` ensures the libvirt network (`vm_network`, default
`default`) is active, then reads its XML to find the real bridge device
(e.g. `virbr0`) and inserts rules via `ansible.builtin.iptables` (idempotent,
checked with `iptables -C`, re-applied every run so it also recovers from a
host reboot resetting the ruleset — it does not persist the rules outside of
Ansible):

- `iptables -I INPUT 1 -i <bridge> -j ACCEPT`
- `iptables -I FORWARD 1 -i <bridge> -j ACCEPT`
- `iptables -I FORWARD 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT`

Some hosts have a host firewall (`iptables-nft`/`nftables`) with a catch-all
`REJECT` at the end of `INPUT` and/or `FORWARD` that pre-dates the libvirt
network, even though `virsh net-list` shows the network as active:

- **`INPUT`** blocks DHCP/DNS requests before they reach `dnsmasq`. Symptom:
  the guest boots fine, cloud-init and NetworkManager correctly retry DHCP
  forever, but `virsh net-dhcp-leases` and `vm_define.yml`'s
  `Wait for VM DHCP lease` task both time out.
- **`FORWARD`** blocks NAT'd guest traffic to the outside world (yum repos,
  downloads) — this is a separate chain from `INPUT` and can be broken
  independently, and *two* rules are needed here, not one: the bridge rule
  only covers the guest's initial outbound packet (`iifname <bridge>`). The
  reply comes back in via the physical NIC, not the bridge, so it needs its
  own accept — unless this host already has a pre-existing
  `ESTABLISHED,RELATED` accept rule in `FORWARD` (it may not, even if
  `INPUT` does). Symptom: DHCP and SSH into the guest work fine, `dnf
  install` inside the guest fails immediately for every repo baseurl with
  `Could not connect to server`, and outbound `ping` from the guest gets an
  explicit `Destination Host Prohibited` back from the gateway.

## Hostname, SSH, and known_hosts

Cloud-init does not map the FQDN to `127.0.0.1` (`manage_etc_hosts: false`).
After DHCP, both the hypervisor and the guest `/etc/hosts` get one unmarked
line with the live NAT IP:

```
192.168.122.95 sap-kvm-vm.lab.eng.tlv2.redhat.com sap-kvm-vm
```

That is required by `community.sap_install.sap_maintain_etc_hosts`, which
fails if `sap-kvm-vm` resolves to `127.0.0.1`. Stale IPs and leftover
`BEGIN`/`END` markers are removed first. Destroy removes the hypervisor line.

Libvirt NAT (`192.168.122.0/24`) is only reachable from the hypervisor. When
the playbook runs against a remote `hypervisor`, guest SSH jumps through that
host (`ProxyCommand` + `sshpass`). On `ansible_connection=local`, SSH is
direct.

Recreating the VM issues new host keys. Post-boot drops old entries in
`/root/.ssh/known_hosts` for the IP, short name, and FQDN, then installs the
current guest keys so interactive `ssh root@sap-kvm-vm.lab.eng.tlv2.redhat.com`
does not hit `REMOTE HOST IDENTIFICATION HAS CHANGED`.

## Requirements

- SSH (or local) access to the KVM hypervisor, with sudo (`become: true`).
- Ansible collections from `collections/requirements.yml`:
  `ansible-galaxy collection install -r collections/requirements.yml`
  (`community.libvirt` ≥ 2.3.0, `community.general` ≥ 8.0.0).
- `vm_root_password` is **required** and is not stored in git.
- Guest image at `guest_image_path`, or `-e download_guest_image=true`.
- Inventory file `inventory_vm.ini` with a single host in `[hypervisor]`.
- `sshpass` on the Ansible controller (and on `hypervisor` when jumping to the
  guest). Use `/usr/libexec/platform-python` on RHEL 9.6 and 10.2.

Tasks use Ansible modules (`community.libvirt.virt` / `virt_pool` /
`virt_volume`, `dnf`, `slurp`, `copy`, `setup`) rather than `command` /
`shell` or custom Python.

## Inventory

Copy the example and edit **one** host in `[hypervisor]`:

```bash
cp inventory_vm.ini.example inventory_vm.ini
```

`inventory_vm.ini` is gitignored so the SSH password is not pushed to GitHub.

Remote hypervisor (password SSH). Set `ansible_host` to a reachable IP or DNS
name, and set `ansible_ssh_pass`. The controller needs `sshpass`
(`dnf install sshpass`):

```ini
[hypervisor]
hypervisor01 ansible_host=192.168.1.10

[all:vars]
ansible_ssh_user=root
ansible_ssh_pass=CHANGE_ME
ansible_python_interpreter=/usr/libexec/platform-python
ansible_ssh_common_args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
```

Run on this machine instead (comment out `hypervisor01`, uncomment localhost):

```ini
[hypervisor]
localhost ansible_connection=local
```

Check connectivity:

```bash
ansible hypervisor -m ping
```

Topology, hugepages, libvirt, and the guest disk are always on **that** host,
not on the machine where you type `ansible-playbook`.

## Run the playbook

From the repository root:

```bash
# Create the VM
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...'

# Recreate from scratch (undefine VM, delete disk, ISO, hosts line, host keys)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e destroy_existing_vm=true

# Download the RHEL guest qcow2 from guest_image_compose_url (also sets the
# guest's first-boot yum repos); compose_url keeps defaulting to the newer
# -updates- compose used to dnf update the guest after boot
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true \
  -e guest_image_compose_url='http://download.eng.tlv.redhat.com/rhel-10/composes/RHEL-10/RHEL-10.2-20260507.1/compose'

# Same, but from the Scale lab mirror instead of TLV (single mirror serves
# both roles there, so point both compose vars at it)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true \
  -e guest_image_compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
  -e compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
  -e compose_release='10.2' \
  -e guest_image_compose_id='10.2-20260408.1'

# Skip the post-boot repo repoint + dnf update, leaving the guest on the
# packages it was installed with
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e guest_update_packages=false

# Cap memory and disk; CPU pinning still follows this host
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e vm_memory_mib=131072 \
  -e vm_disk_size=150G

# Run the guest off a raw disk instead of qcow2 (converts after disk prep;
# shuts the VM down first if it was already running qcow2)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e guest_raw_image=true

# Limit vCPUs (must divide evenly by NUMA nodes × threads)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e vm_vcpus=20

# Attach an unused hypervisor NVMe device to the guest via PCI passthrough
# (vdb) instead of a local qcow2 data disk. IOMMU is enabled automatically
# as part of host tuning; no manual grubby/reboot step is needed.
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true \
  -e destroy_existing_vm=true \
  -e vm_nvme=true \
  -e guest_raw_image=true \
  -e vm_disk_size=340G

# Explicit size for the local qcow2 data disk (vm_nvme=false, the default)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e vm_data_disk_size=500G

# Only preflight + hugepages (topology discovery always runs)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  --tags preflight,hugepages
```

Useful extra-vars:

| Variable | Default | Purpose |
|---|---|---|
| `vm_root_password` | (required) | Guest root password; never stored in git |
| `guest_image_compose_url` | TLV RHEL-10.2-20260507.1 (GA) compose | Sources the qcow2 and first-boot guest repos; needs `BaseOS/x86_64/images/` |
| `compose_url` | TLV RHEL-10.2-updates-20261001.1 compose | Sources the repos the guest is repointed at after boot, then `dnf update` |
| `compose_release` | `10.2` | Repo names/descriptions; independent of both compose URLs |
| `guest_image_compose_id` | `10.2-20260507.1` | Dated build id in the qcow2 filename; pair with `guest_image_compose_url`, never with `compose_url` (see `-updates-*` caveat above) |
| `download_guest_image` | `false` | Fetch qcow2 from `guest_image_url` |
| `guest_image_filename` | `rhel10-2-base.qcow2` | Local image name only |
| `guest_update_packages` | `true` | Repoint guest repos at `compose_url` and `dnf update` after boot |
| `guest_raw_image` | `false` | Run the guest off `vm_disk_raw_path` (raw) instead of `vm_disk_path` (qcow2) |
| `guest_raw_image_remove_qcow2` | `false` | Delete the pre-conversion qcow2 after a successful raw conversion |
| `vm_nvme` | `false` | Attach the secondary data disk (`vdb`) as an NVMe PCI passthrough device instead of a local qcow2 file |
| `vm_nvme_device` | `/dev/nvme0n1` | Hypervisor NVMe namespace reset before PCI passthrough when `vm_nvme=true` |
| `vm_data_disk_size` | `vm_disk_size` | Size of the local qcow2 data disk when `vm_nvme=false` |
| `vm_data_disk_path` | `{kvm_base_dir}/{vm_name}-data.qcow2` | Path of the local qcow2 data disk when `vm_nvme=false` |
| `cloud_init_boot_timeout` | `1800` | Seconds to wait for cloud-init's `boot-finished` marker |
| `destroy_existing_vm` | `false` | Tear down VM, disk(s), ISO, `/etc/hosts`, known_hosts |
| `vm_domain` | `lab.eng.tlv2.redhat.com` | FQDN suffix for `/etc/hosts` |
| `vm_memory_min_gib` | `64` | Floor for auto-sized guest RAM |
| `vm_memory_prefault` | `true` | Add `<allocation mode='immediate'/>` to pre-fault all guest memory at boot |
| `vm_host_reboot_timeout` | `600` | Seconds to wait for the hypervisor to come back after `host_tuning.yml` reboots it |
| `vm_kvm_halt_poll_ns` | `2000000` | `kvm` module `halt_poll_ns` (host-wide) |
| `vm_kvm_lapic_timer_advance_ns` | `1000` | `kvm` module `lapic_timer_advance_ns` (host-wide) |
| `vm_kvm_ple_gap` | `0` | `kvm_intel` module `ple_gap` (host-wide; skipped on AMD) |

Tags: `preflight`, `cleanup`, `topology` (always), `host_tuning`,
`kvm_tuning`, `hugepages`, `disk`, `raw_convert`, `data_disk`, `nvme`,
`nvme_cleanup`, `cloud_init`, `vm_define`, `post_boot`, `repo_update`,
`verify`.

After a successful run, from the hypervisor:

```bash
ssh root@sap-kvm-vm.lab.eng.tlv2.redhat.com
```

The summary prints IP, vCPUs, memory, NUMA nodes, and disk (not the password).
