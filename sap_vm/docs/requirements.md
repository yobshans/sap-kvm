# Requirements

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
