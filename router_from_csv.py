#!/usr/bin/env python3
"""
Genereer een Cisco broadband-router configuratie (tekstbestand om op de CLI te plakken)
op basis van een CSV-bestand.

CSV-header: network, interface, description, vlan, ipaddress, subnetmask, defaultgateway

Ondersteunde scenario's:
  1. WAN via DHCP  + 1 LAN                          (ip address dhcp)
  2. WAN statisch  + 1 LAN                          (default route naar defaultgateway)
  3. LAN-rijen zonder interface = netwerken achter een interne router/L3-switch
                                                    (statische route via defaultgateway)
  4. LAN-rijen met vlan > 0 op dezelfde interface   (router-on-a-stick, subinterfaces)

Alle LAN-netwerken worden via PAT (NAT overload) naar de WAN-interface vertaald.

Voorbeelden:
  python router_from_csv.py examples/scenario4-roas.csv
  python router_from_csv.py examples/scenario3-static-routes.csv -o router.txt --hostname R1 --dhcp
"""
import argparse
import csv
import ipaddress
import sys
from pathlib import Path

NAT_ACL = "NAT_INSIDE"


def read_rows(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
    rows = []
    for raw in csv.DictReader(text.splitlines(), dialect=dialect):
        row = {(k or "").strip().lower().replace(" ", ""): (v or "").strip() for k, v in raw.items()}
        if any(row.values()):
            rows.append(row)
    return rows


def wildcard(mask: str):
    return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(mask)) ^ 0xFFFFFFFF))


def network_of(ip: str, mask: str):
    return ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)


def build_config(rows, hostname=None, dhcp_pools=False):
    wan = [r for r in rows if r.get("network", "").lower() == "wan"]
    lan = [r for r in rows if r.get("network", "").lower() == "lan"]
    if len(wan) != 1:
        raise ValueError(f"Precies 1 WAN-rij verwacht, gevonden: {len(wan)}")
    if not lan:
        raise ValueError("Geen LAN-rijen gevonden")
    wan = wan[0]

    cfg = ["enable", "configure terminal"]
    if hostname:
        cfg.append(f"hostname {hostname}")
    cfg += ["no ip domain-lookup", "!"]

    # --- WAN ---
    cfg += [f"interface {wan['interface']}", f" description {wan.get('description') or 'WAN'}"]
    if wan.get("ipaddress", "").lower() == "dhcp":
        cfg.append(" ip address dhcp")
    else:
        cfg.append(f" ip address {wan['ipaddress']} {wan['subnetmask']}")
    cfg += [" ip nat outside", " no shutdown", "!"]

    nat_networks = []
    routes = []
    trunk_parents = set()

    # --- LAN ---
    for row in lan:
        net = network_of(row["ipaddress"], row["subnetmask"])
        nat_networks.append(net)
        iface = row.get("interface")
        vlan = int(row.get("vlan") or 0)

        if not iface:
            # Netwerk achter een interne router: enkel route + NAT
            if not row.get("defaultgateway"):
                raise ValueError(f"LAN '{row.get('description')}' zonder interface heeft een defaultgateway nodig")
            routes.append(f"ip route {net.network_address} {net.netmask} {row['defaultgateway']}")
            continue

        if vlan > 0:
            if iface not in trunk_parents:
                trunk_parents.add(iface)
                cfg += [f"interface {iface}", " description Trunk naar switch", " no ip address", " no shutdown", "!"]
            cfg += [
                f"interface {iface}.{vlan}",
                f" description {row.get('description', '')}",
                f" encapsulation dot1Q {vlan}",
            ]
        else:
            cfg += [f"interface {iface}", f" description {row.get('description', '')}"]
        cfg += [f" ip address {row['ipaddress']} {row['subnetmask']}", " ip nat inside"]
        if vlan == 0:
            cfg.append(" no shutdown")
        cfg.append("!")

        if dhcp_pools:
            name = (row.get("description") or f"LAN{vlan}").replace(" ", "_")
            cfg += [
                f"ip dhcp excluded-address {row['ipaddress']}",
                f"ip dhcp pool {name}",
                f" network {net.network_address} {net.netmask}",
                f" default-router {row['ipaddress']}",
                f" dns-server {row['ipaddress']} 1.1.1.1",
                "!",
            ]

    # --- NAT (PAT) ---
    cfg.append(f"ip access-list standard {NAT_ACL}")
    for net in nat_networks:
        cfg.append(f" permit {net.network_address} {wildcard(str(net.netmask))}")
    cfg += ["!", f"ip nat inside source list {NAT_ACL} interface {wan['interface']} overload", "!"]

    # --- Routing ---
    if wan.get("ipaddress", "").lower() != "dhcp":
        if not wan.get("defaultgateway"):
            raise ValueError("Statisch WAN-adres zonder defaultgateway")
        cfg.append(f"ip route 0.0.0.0 0.0.0.0 {wan['defaultgateway']}")
    # (bij DHCP installeert IOS zelf de default route)
    cfg += routes
    if dhcp_pools:
        cfg.append("ip dns server")
    cfg += ["!", "end", "write memory"]
    return cfg


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", type=Path)
    p.add_argument("-o", "--output", type=Path, help="tekstbestand (standaard: <csv>.txt)")
    p.add_argument("--hostname")
    p.add_argument("--dhcp", action="store_true", help="maak ook DHCP-pools voor de LANs met een interface")
    p.add_argument("--stdout", action="store_true", help="enkel naar het scherm")
    args = p.parse_args()

    try:
        config = "\n".join(build_config(read_rows(args.csv), args.hostname, args.dhcp)) + "\n"
    except (ValueError, KeyError) as e:
        sys.exit(f"Fout in {args.csv}: {e}")

    if args.stdout:
        print(config, end="")
        return
    out = args.output or args.csv.with_suffix(".txt")
    out.write_text(config)
    print(f"Config geschreven naar {out}")


if __name__ == "__main__":
    main()
