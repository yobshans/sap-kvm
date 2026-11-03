# Inventory

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
