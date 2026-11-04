# Guest network / DHCP lease and outbound access

`preflight.yml` ensures the libvirt network (`vm_network`, default
`default`) is active, then reads its XML to find the real bridge device
(e.g. `virbr0`) and inserts rules directly into the host’s nftables
`ip filter INPUT` and `ip filter FORWARD` chains:

- `iifname "<bridge>" udp dport { 53, 67 } accept`
- `iifname "<bridge>" tcp dport 53 accept`
- `ct state established,related accept`
- `iifname "<bridge>" accept`

These rules are reapplied on every run, so they also recover from a host
reboot or firewall reload. They are not persisted outside Ansible.

Some hosts have a host firewall with a catch-all `REJECT` at the end of
`INPUT` and/or `FORWARD` that pre-dates the libvirt network, even though
`virsh net-list` shows the network as active:

- **`INPUT`** blocks DHCP/DNS requests before they reach `dnsmasq`. Symptom:
  the guest boots fine, cloud-init and NetworkManager correctly retry DHCP
  forever, but `virsh net-dhcp-leases` and `vm_define.yml`'s
  `Wait for VM DHCP lease` task both time out.
- **`FORWARD`** blocks NAT'd guest traffic to the outside world (yum repos,
  downloads) — this is a separate chain from `INPUT` and can be broken
  independently. Symptom: DHCP and SSH into the guest work fine, `dnf
  install` inside the guest fails immediately with `Could not connect to
  server`, and outbound traffic is rejected or prohibited.
