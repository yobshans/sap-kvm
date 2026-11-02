# How VM size is calculated

Discovery reads sysfs with Ansible (`find`, `slurp`) and renders
`templates/hana_topology.j2`. Results are applied as facts and written to
`/home/kvm/<vm_name>-topology.json`.

Set a variable to `auto` (the default) to derive it. Pass a concrete extra-var
to override; pinning still comes from the live host topology.

## CPU and NUMA

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

## Memory and hugepages

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

The role sizes the guest to the available NUMA memory and reserves the matching
number of 1 GiB hugepages. The total reservation is persisted in
`/etc/sysctl.d/sap-hana-hugepages.conf`, applied immediately with `sysctl -p`,
and the live per-NUMA values are written after all host-side NVMe cleanup so a
later `sysctl --system` cannot reset them to zero. Before reserving them, the
role stops host-side SAP HANA services, terminates any leftover HANA or PBO
processes, waits for them to exit, drops page/slab caches, and asks the kernel
to compact memory. Hugepage allocation is retried with cache drop and
compaction between attempts. If enough free hugepages still cannot be provided,
the role fails before VM start with the per-NUMA `nr_hugepages` and
`free_hugepages` values.

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

## Disk

OS disk default is 150 GiB (`vm_disk_preferred_gib`), or less if the filesystem
under `/home/kvm` is tight (`vm_disk_reserve_gib` left free). Override with
`vm_disk_size` (for example `150G`).

If a libvirt pool already owns `kvm_base_dir` (this host uses pool `kvm` on
`/home/kvm`), that pool is reused instead of creating `sap-kvm`.

### Raw disk format (`guest_raw_image`)

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

### Secondary data disk vs. NVMe PCI passthrough (`vm_nvme`)

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

## Clock and CPU flags

TSC frequency is discovered from the host (`tsc_freq_khz` or kernel
calibration). The domain has a single `<timer name='tsc'/>`. If the CPU has
no TSC scaling and the value is still out of range, libvirt's advertised host
frequency is applied and start is retried. Override with
`-e vm_tsc_frequency=...` only on hosts that support TSC scaling.

The domain uses host-passthrough, L3 cache emulation, and `rdtscp` / `invtsc` /
`x2apic`. Memory ballooning is disabled.
