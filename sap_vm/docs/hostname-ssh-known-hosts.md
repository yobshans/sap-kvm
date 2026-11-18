# Hostname, SSH, and known_hosts

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
