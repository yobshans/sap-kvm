# sap_vm

Ansible role that creates a KVM guest for SAP HANA / HCMT benchmarking. On each
run it reads the hypervisor (CPU, NUMA, memory, disk, TSC) and builds a libvirt
domain from that hardware, following SAP HANA on KVM / RHV sizing rules.

The wrapper playbook is `create_sap_vm.yml` at the repository root. It targets
the **`kvm_host`** inventory group (this machine or a remote hypervisor).

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
the IO thread (`iothreadpin`). Remaining cores are pinned 1:1: each guest
thread pair of a core shares the host SMT sibling cpuset.

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

Inside the guest, transparent hugepages are set to `never`
(`/sys/kernel/mm/transparent_hugepage/enabled`) and persisted with
`/etc/tmpfiles.d/sap-thp.conf`.

### Disk

OS disk default is 150 GiB (`vm_disk_preferred_gib`), or less if the filesystem
under `/home/kvm` is tight (`vm_disk_reserve_gib` left free). Override with
`vm_disk_size` (for example `150G`).

If a libvirt pool already owns `kvm_base_dir` (this host uses pool `kvm` on
`/home/kvm`), that pool is reused instead of creating `sap-kvm`.

### Clock and CPU flags

TSC frequency is discovered from the host (`tsc_freq_khz` or kernel
calibration). The domain has a single `<timer name='tsc'/>`. If the CPU has
no TSC scaling and the value is still out of range, libvirt's advertised host
frequency is applied and start is retried. Override with
`-e vm_tsc_frequency=...` only on hosts that support TSC scaling.

The domain uses host-passthrough, L3 cache emulation, and `rdtscp` / `invtsc` /
`x2apic`. Memory ballooning is disabled.

## RHEL compose (image and guest repos)

`compose_url` must point at the directory that directly contains the Variant
directories (`BaseOS`, `AppStream`, `CRB`, `SAP`, `SAPHANA`). Different labs
put that at different depths, so `compose_url` includes whatever prefix your
lab needs:

| Lab | `compose_url` |
|---|---|
| TLV (default) | `http://download.eng.tlv.redhat.com/rhel-10/composes/RHEL-10/RHEL-10.2-20260507.1/compose` |
| Scale lab | `http://mirror.scalelab.redhat.com/RHEL10/10.2` (no `compose/` subdir) |

`compose_release` (default `10.2`, used for repo names/descriptions) and
`guest_image_compose_id` (default `10.2-20260507.1`, the exact dated build id
embedded in the qcow2 filename) are **independent** of `compose_url` — a
mirror's URL does not necessarily encode either the same way a compose's
does, and a lab like Scale lab mirrors a moving "latest" build under a
release-only path (`.../10.2/`) with no date in the URL at all. Override all
three together when switching labs:

```bash
-e compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
-e compose_release='10.2' \
-e guest_image_compose_id='10.2-20260408.1'
```

| Derived value | Rule |
|---|---|
| Remote qcow2 | `{compose_url}/BaseOS/x86_64/images/rhel-guest-image-{guest_image_compose_id}.x86_64.qcow2` |
| Local copy | `guest_image_path`, default `/home/kvm/rhel10-2-base.qcow2` (`guest_image_filename`) |
| Repos | BaseOS, AppStream, CRB, SAP, SAPHANA under `{compose_url}/<Name>/x86_64/os/`, each `skip_if_unavailable` so a lab missing SAP/SAPHANA just skips those two |

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
- Yum repos from `compose_url`, packages from `vm_packages` (includes
  `libxcrypt-compat` for HCMT) plus `qemu-guest-agent`
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

## Guest network / DHCP lease and outbound access

`preflight.yml` ensures the libvirt network (`vm_network`, default
`default`) is active, then reads its XML to find the real bridge device
(e.g. `virbr0`) and inserts two rules via `ansible.builtin.iptables`
(idempotent, checked with `iptables -C`, re-applied every run so it also
recovers from a host reboot resetting the ruleset — it does not persist the
rules outside of Ansible):

- `iptables -I INPUT 1 -i <bridge> -j ACCEPT`
- `iptables -I FORWARD 1 -i <bridge> -j ACCEPT`

Some hosts have a host firewall (`iptables-nft`/`nftables`) with a catch-all
`REJECT` at the end of `INPUT` and/or `FORWARD` that pre-dates the libvirt
network, even though `virsh net-list` shows the network as active:

- **`INPUT`** blocks DHCP/DNS requests before they reach `dnsmasq`. Symptom:
  the guest boots fine, cloud-init and NetworkManager correctly retry DHCP
  forever, but `virsh net-dhcp-leases` and `vm_define.yml`'s
  `Wait for VM DHCP lease` task both time out.
- **`FORWARD`** blocks NAT'd guest traffic to the outside world (yum repos,
  downloads) — this is a separate chain from `INPUT` and can be broken
  independently. Symptom: DHCP and SSH into the guest work fine, but
  `dnf install` inside the guest fails immediately for every repo baseurl
  with `Could not connect to server`. Only the guest → outside direction
  needs an explicit `ACCEPT`; reply traffic already matches the host's
  pre-existing `ESTABLISHED,RELATED` allow rule.

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
the playbook runs against a remote `kvm_host`, guest SSH jumps through that
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
- Inventory file `inventory_vm.ini` with a single host in `[kvm_host]`.
- `sshpass` on the Ansible controller (and on `kvm_host` when jumping to the
  guest). Use `/usr/libexec/platform-python` on RHEL 9.6 and 10.2.

Tasks use Ansible modules (`community.libvirt.virt` / `virt_pool` /
`virt_volume`, `dnf`, `slurp`, `copy`, `setup`) rather than `command` /
`shell` or custom Python.

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

# Recreate from scratch (undefine VM, delete disk, ISO, hosts line, host keys)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e destroy_existing_vm=true

# Download the RHEL guest qcow2 from compose_url (also sets guest yum repos)
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true \
  -e compose_url='http://download.eng.tlv.redhat.com/rhel-10/composes/RHEL-10/RHEL-10.2-20260507.1/compose'

# Same, but from the Scale lab mirror instead of TLV
ansible-playbook -vv create_sap_vm.yml -i inventory_vm.ini \
  -e vm_root_password='...' \
  -e download_guest_image=true \
  -e compose_url='http://mirror.scalelab.redhat.com/RHEL10/10.2' \
  -e compose_release='10.2' \
  -e guest_image_compose_id='10.2-20260408.1'

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

Useful extra-vars:

| Variable | Default | Purpose |
|---|---|---|
| `vm_root_password` | (required) | Guest root password; never stored in git |
| `compose_url` | TLV RHEL-10.2-20260507.1 compose | Directory containing BaseOS/AppStream/CRB/SAP/SAPHANA |
| `compose_release` | `10.2` | Repo names/descriptions; independent of `compose_url` |
| `guest_image_compose_id` | `10.2-20260507.1` | Dated build id in the qcow2 filename; independent of `compose_url` |
| `download_guest_image` | `false` | Fetch qcow2 from `guest_image_url` |
| `guest_image_filename` | `rhel10-2-base.qcow2` | Local image name only |
| `cloud_init_boot_timeout` | `1800` | Seconds to wait for cloud-init's `boot-finished` marker |
| `destroy_existing_vm` | `false` | Tear down VM, disk, ISO, `/etc/hosts`, known_hosts |
| `vm_domain` | `lab.eng.tlv2.redhat.com` | FQDN suffix for `/etc/hosts` |
| `vm_memory_min_gib` | `64` | Floor for auto-sized guest RAM |

Tags: `preflight`, `cleanup`, `topology` (always), `hugepages`, `disk`,
`cloud_init`, `vm_define`, `post_boot`, `verify`.

After a successful run, from the hypervisor:

```bash
ssh root@sap-kvm-vm.lab.eng.tlv2.redhat.com
```

The summary prints IP, vCPUs, memory, NUMA nodes, and disk (not the password).
