# sap_vm

Ansible role that creates a KVM guest for SAP HANA benchmarking. On each run it
reads the hypervisor (CPU, NUMA, memory, disk, TSC) and builds a libvirt domain
from that hardware, following SAP HANA on KVM / RHV sizing rules.

The wrapper playbook is `create_sap_vm.yml` at the repository root. It targets
the **`kvm_host`** inventory group (this machine or a remote hypervisor).

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
the IO thread (`iothreadpin`). Remaining cores are pinned 1:1: each guest
thread pair of a core shares the host SMT sibling cpuset.

Example: 2 sockets, 20 cores/socket, SMT-2 → reserve 1 core/node →  
`2 × 19 × 2 = 76` vCPUs, 2 NUMA cells.

`--vcpus` / `vm_vcpus` must be divisible by `numa_nodes × threads`.

### Memory and hugepages

Guest memory is balanced across NUMA cells and aligned to the hugepage size
(prefer **1 GiB**, fall back to 2 MiB if the kernel has no 1 GiB pool).

1. Read `MemTotal` per host NUMA node.
2. Reserve hypervisor RAM: default **100 GiB** total
   (`hypervisor_reserve_gib`), split across nodes, at least
   `hypervisor_reserve_min_gib_per_node` (2 GiB) per node.
3. On small hosts, if that reserve would leave less than one hugepage per
   node, the reserve is reduced (about 1/8 of node RAM, still ≥ 2 GiB when
   possible).
4. Per-node guest RAM = `min(node MemTotal) − per-node reserve`, rounded down
   to a whole number of hugepages.
5. Total guest RAM = per-node RAM × NUMA nodes.

1 GiB pages usually cannot be allocated on a fragmented running system. If
runtime reservation fails, the role stops and prints `grubby` commands. Set
`nr_hugepages` on the kernel command line, **reboot**, then re-run.

### Disk

OS disk default is 150 GiB (`vm_disk_preferred_gib`), or less if the filesystem
under `/home/kvm` is tight (`vm_disk_reserve_gib` left free). Override with
`vm_disk_size` (for example `150G`).

### Clock and CPU flags

TSC frequency is discovered from the host (`tsc_freq_khz` or kernel
calibration). The domain has a single `<timer name='tsc'/>`. If the CPU has
no TSC scaling and the value is still out of range, libvirt's advertised host
frequency is applied and start is retried. Override with
`-e vm_tsc_frequency=...` only on hosts that support TSC scaling.

The domain uses host-passthrough, L3 cache emulation, and `rdtscp` / `invtsc` /
`x2apic`. Memory ballooning is disabled.

## Requirements

- SSH (or local) access to the KVM hypervisor, with sudo (`become: true`).
- Ansible collections from `collections/requirements.yml`:
  `ansible-galaxy collection install -r collections/requirements.yml`
- `vm_root_password` is **required** and is not stored in git.
- Guest image at `kvm_base_dir` / `guest_image_filename`, or
  `-e download_guest_image=true`.
- Inventory file `inventory_vm.ini` with a single host in `[kvm_host]`.

Tasks use Ansible modules (`community.libvirt.virt`, `community.general.iso_create`,
`dnf`, `slurp`, `setup`) rather than `command` / `shell` or custom Python.
Guest yum repos, packages, SSH, and filesystem grow are applied by cloud-init
on first boot.

## Inventory

Copy the example and edit **one** host in `[kvm_host]`:

```bash
cp inventory_vm.ini.example inventory_vm.ini
```

`inventory_vm.ini` is gitignored so the SSH password is not pushed to GitHub.

Remote hypervisor (password SSH). Set `ansible_host` to a reachable IP or DNS
name, and set `ansible_ssh_pass`. The controller needs `sshpass`
(`dnf install sshpass`):

```ini
[kvm_host]
kvmhost ansible_host=192.168.1.10

[all:vars]
ansible_ssh_user=root
ansible_ssh_pass=CHANGE_ME
ansible_python_interpreter=/usr/libexec/platform-python
ansible_ssh_common_args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
```

Run on this machine instead (comment out `kvmhost`, uncomment localhost):

```ini
[kvm_host]
localhost ansible_connection=local
```

Check connectivity:

```bash
ansible kvm_host -m ping
```

Topology, hugepages, libvirt, and the guest disk are always on **that** host,
not on the machine where you type `ansible-playbook`.

## Run the playbook

From the repository root:

```bash
# Create the VM
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...'

# Recreate from scratch (undefine VM, delete disk and cloud-init ISO)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e destroy_existing_vm=true

# Download the RHEL guest qcow2 first
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true

# Cap memory and disk; CPU pinning still follows this host
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e vm_memory_mib=131072 \
  -e vm_disk_size=150G

# Limit vCPUs (must divide evenly by NUMA nodes × threads)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e vm_vcpus=20

# Only preflight + hugepages (topology discovery always runs)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  --tags preflight,hugepages
```

Tags: `preflight`, `cleanup`, `topology` (always), `hugepages`, `disk`,
`cloud_init`, `vm_define`, `post_boot`, `verify`.

After a successful run, SSH as `root@<guest-ip>` with `vm_root_password`.
The summary prints `Password: *****`.
