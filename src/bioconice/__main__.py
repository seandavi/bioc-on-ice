import argparse

from . import catalog, ensembl


def main():
    p = argparse.ArgumentParser(prog="bioconice", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest-ensembl", help="load one species of one Ensembl release")
    ing.add_argument("species", help="e.g. homo_sapiens")
    ing.add_argument("--release", default="116")

    sub.add_parser("tables", help="list catalog tables")
    args = p.parse_args()

    cat = catalog()
    if args.cmd == "ingest-ensembl":
        for name, rows in ensembl.ingest(cat, args.release, args.species).items():
            print(f"{name:35} {rows:>10,} rows")
    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
