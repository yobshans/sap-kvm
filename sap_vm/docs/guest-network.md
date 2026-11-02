# Guest network / DHCP lease and outbound access

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
