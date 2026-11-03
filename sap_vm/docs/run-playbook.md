# Run the playbook

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
| `guest_image_compose_id` | `10.2-20260507.1` | Dated build id in the qcow2 filename; pair with `guest_image_compose_url`, never with `compose_url` (see the `-updates-*` caveat in the [RHEL compose guide](rhel-compose.md)) |
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
| `vm_hugepage_prepare_memory` | `true` | Drop caches and compact memory before allocating 1 GiB hugepages |
| `vm_host_memory_cleanup` | `true` | Stop host-side SAP HANA and leftover PBO processes before hugepage allocation |
| `vm_host_reboot_timeout` | `600` | Seconds to wait for the hypervisor to come back after `host_tuning.yml` reboots it |
| `vm_kvm_halt_poll_ns` | `2000000` | `kvm` module `halt_poll_ns` (host-wide) |
| `vm_kvm_lapic_timer_advance_ns` | `1000` | `kvm` module `lapic_timer_advance_ns` (host-wide) |
| `vm_kvm_ple_gap` | `0` | `kvm_intel` module `ple_gap` (host-wide; skipped on AMD) |

Tags: `preflight`, `cleanup`, `topology` (always), `host_tuning`,
`memory_cleanup`, `kvm_tuning`, `hugepages`, `disk`, `raw_convert`,
`data_disk`, `nvme`, `nvme_cleanup`, `cloud_init`, `vm_define`, `post_boot`,
`repo_update`, `verify`.

After a successful run, from the hypervisor:

```bash
ssh root@sap-kvm-vm.lab.eng.tlv2.redhat.com
```

The summary prints IP, vCPUs, memory, NUMA nodes, and disk (not the password).
