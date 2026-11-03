# sap_vm

Ansible role that creates a KVM guest for SAP HANA / HCMT benchmarking. On each
run it reads the hypervisor (CPU, NUMA, memory, disk, TSC) and builds a libvirt
domain from that hardware, following SAP HANA on KVM / RHV sizing rules.

The wrapper playbook is `create_sap_vm.yml` at the repository root. It targets
the **`hypervisor`** inventory group (this machine or a remote hypervisor).

**Guest OS** is RHEL 10.2. The **hypervisor** can be RHEL 9.6 or 10.2.

## Documentation

### Sizing and hardware

- [How VM size is calculated](docs/vm-sizing.md) — CPU, NUMA, memory,
  hugepages, disks, NVMe passthrough, and clock configuration.
- [Hypervisor host tuning](docs/hypervisor-host-tuning.md) — CPU isolation,
  static hugepages, CPU mitigations, and KVM module tuning.

### Guest configuration

- [RHEL compose](docs/rhel-compose.md) — image, install-time repositories,
  and post-boot update repositories.
- [First boot with cloud-init](docs/first-boot-cloud-init.md) — guest network,
  identity, packages, and filesystem setup.
- [Package update](docs/package-update.md) — repoint repositories and update
  the guest after first boot.
- [Guest network](docs/guest-network.md) — DHCP, outbound access, and
  libvirt bridge firewall rules.
- [Hostname, SSH, and known_hosts](docs/hostname-ssh-known-hosts.md) —
  `/etc/hosts`, SSH access, and stale host-key cleanup.

### Getting started

- [Requirements](docs/requirements.md) — controller, hypervisor, collections,
  guest image, and credentials.
- [Inventory](docs/inventory.md) — configure the `[hypervisor]` inventory.
- [Run the playbook](docs/run-playbook.md) — create, recreate, customize, and
  verify the KVM guest.
