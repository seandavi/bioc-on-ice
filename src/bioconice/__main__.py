import argparse

from . import bedbase
from . import biogrid
from . import bugsigdb
from . import catalog
from . import cellosaurus
from . import cellxgene
from . import complexportal
from . import encode
from . import ensembl
from . import eqtlcatalogue
from . import gwas_catalog
from . import hgnc
from . import icite
from . import intact
from . import mane
from . import ncbi
from . import ncbi_accession
from . import ncbi_go
from . import ncbi_orthologs
from . import ncbi_pubmed
from . import obo
from . import pubtator3
from . import rnacentral
from . import wikipathways


def _print(counts):
    for name, c in counts.items():
        print(f"{name:40} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged"
              if isinstance(c, dict) else f"{name:40} {c:>10,} rows")


def main():
    p = argparse.ArgumentParser(prog="bioconice")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest-ensembl", help="land one species of one Ensembl release, then derive")
    ing.add_argument("species", help="e.g. homo_sapiens")
    ing.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    ing.add_argument("--ensembl-release", default="116")
    ing.add_argument("--transform-only", action="store_true",
                     help="re-derive from already-landed raw rows, without re-downloading")

    bs = sub.add_parser("ingest-bugsigdb", help="land a BugSigDB export release (no transform yet)")
    bs.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
    bs.add_argument("--version", default=bugsigdb.DEFAULT_VERSION,
                    help="BugSigDBExports release tag, e.g. v1.3.1. Tags are immutable; "
                         "the devel branch re-exports hourly and is not")

    # The NCBI ingests share a CLI shape: land whole, then derive — every taxon
    # in the dump by default, or only the ones named.
    ncbi_cmds = {
        "ingest-ncbi": (ncbi, "land the NCBI Gene dumps whole, then derive"),
        "ingest-ncbi-accession": (ncbi_accession, "land NCBI gene2accession whole, then derive"),
        "ingest-ncbi-pubmed": (ncbi_pubmed, "land NCBI gene2pubmed whole, then derive"),
        "ingest-gene2go": (ncbi_go, "land NCBI gene2go whole, then derive GO annotations"),
        "ingest-ncbi-orthologs": (ncbi_orthologs, "land NCBI gene_orthologs and gene_group whole, "
                                  "then derive ortholog pairs"),
    }
    for cmd, (_, help_text) in ncbi_cmds.items():
        c = sub.add_parser(cmd, help=help_text)
        c.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.08")
        c.add_argument("--taxa", help="comma-separated taxa to DERIVE annotation for "
                       "(default: every taxon in the dump); raw is always landed whole")

    ic = sub.add_parser("ingest-icite", help="land the monthly iCite snapshot whole, then derive")
    ic.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    ic.add_argument("--snapshot", help="iCite snapshot label, e.g. 2026-08 (default: latest on Figshare)")
    ic.add_argument("--csv", help="an already-extracted icite_metadata.csv; skips download")

    pt = sub.add_parser("ingest-pubtator3",
                        help="land the five PubTator3 entity dumps whole, then derive mentions")
    pt.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    pt.add_argument("--url", help="alternate directory or URL prefix (with trailing slash) holding "
                    "the five <kind>2pubtator3.gz files; default is NCBI's FTP directory")

    cx = sub.add_parser("ingest-cellxgene", help="land the CELLxGENE Discover dataset listing whole, then derive")
    cx.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    cx.add_argument("--json", help="an already-fetched datasets listing JSON; skips the API call")
    cx.add_argument("--census-release", help="Census build to reference, e.g. 2025-11-08 "
                     "(default: the release manifest's 'stable' LTS alias)")
    ob = sub.add_parser("ingest-obo", help="land one OBO ontology's release, then derive term + relationship")
    ob.add_argument("ontology", choices=sorted(obo.REGISTRY),
                    help="cl, uberon, mondo, efo, hsapdv, mmusdv, go, doid")
    ob.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    ob.add_argument("--url", help="an already-downloaded OBO Graphs JSON file or alternate URL; "
                    "skips the registry URL (how offline tests stay offline)")

    bb = sub.add_parser("ingest-bedbase",
                        help="land BEDbase's newest monthly Parquet snapshot whole, then derive "
                             "resource entries")
    bb.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")

    hg = sub.add_parser("ingest-hgnc", help="land the HGNC complete set whole, then derive")
    hg.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    hg.add_argument("--url", help="a dated archive (hgnc_complete_set_YYYY-MM-DD.txt, citable: its "
                    "date becomes the version) or a local copy; default is the rolling file, "
                    "versioned by retrieval date")

    mn = sub.add_parser("ingest-mane", help="land the MANE summary whole, then derive")
    mn.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    mn.add_argument("--url", help="a specific release's MANE.GRCh38.vX.Y.summary.txt.gz, or a local "
                    "copy; the version is read from the file name (default: the newest release)")

    cs = sub.add_parser("ingest-cellosaurus", help="land the Cellosaurus flat file whole, then derive")
    cs.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    cs.add_argument("--url", help="a local or alternate cellosaurus.txt; default is the current "
                    "release on the Expasy FTP site. The version is read from the file either way")
    wp = sub.add_parser("ingest-wikipathways",
                        help="land every species GMT of one WikiPathways release, then derive")
    wp.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    wp.add_argument("--url", help="a dated archive directory (e.g. https://data.wikipathways.org/"
                    "20260810/gmt/) or a local directory of .gmt files; default is current/gmt/")
    eq = sub.add_parser("ingest-eqtlcatalogue",
                        help="land the eQTL Catalogue dataset metadata and FTP paths whole, then "
                             "derive resource entries (summary statistics are referenced)")
    eq.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    eq.add_argument("--url", help="root of a copy of eQTL-Catalogue-resources — another git ref on "
                    "raw.githubusercontent.com, or a local checkout; default is tag v26.09.2")
    eq.add_argument("--eqtl-release", help="eQTL Catalogue release to land, the N of "
                    f"dataset_metadata_rN.tsv (default: {eqtlcatalogue.RELEASE})")

    gw = sub.add_parser("ingest-gwas-catalog",
                        help="land the GWAS Catalog associations and studies whole, then derive")
    gw.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    gw.add_argument("--url", help="a Catalog release directory (…/releases/2026/09/15/, citable: its "
                    "date becomes the version) or a local copy holding both files; default is "
                    "the newest dated directory")

    cp = sub.add_parser("ingest-complexportal", help="land every Complex Portal complextab file, then derive")
    cp.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    cp.add_argument("--complexportal-release", help="dated release, e.g. 2026-01-09 (default: newest on the FTP)")
    cp.add_argument("--url", help="a <YYYY-MM-DD>/complextab/ directory elsewhere, or a local copy "
                    "laid out the same way; the dated directory states the version")

    en = sub.add_parser("ingest-encode",
                        help="land the ENCODE portal's experiment and file inventory whole, then derive")
    en.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    en.add_argument("--experiments", help="an already-downloaded Experiment report.tsv; skips that download")
    en.add_argument("--files", help="an already-downloaded File report.tsv; skips that download")
    rc = sub.add_parser("ingest-rnacentral",
                        help="land RNAcentral's id mapping whole, then derive ncRNA cross-references")
    rc.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    rc.add_argument("--taxa", help="comma-separated taxa to DERIVE annotation for (default: every "
                    "taxon in the file, in taxon-range shards); raw is always landed whole")
    rc.add_argument("--rnacentral-release", help="RNAcentral release number, e.g. 27 "
                    "(default: whatever current_release is)")
    rc.add_argument("--url", help="an already-downloaded id_mapping.tsv.gz or a mirror, instead of "
                    "EBI's releases/NN.0/; needs --rnacentral-release to say which release it is")

    bg = sub.add_parser("ingest-biogrid", help="land the BioGRID ALL tab3 release whole, then derive")
    bg.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    bg.add_argument("--biogrid-release", help="BioGRID release, e.g. 5.0.261 (default: newest in Release-Archive)")
    bg.add_argument("--url", help="a BIOGRID-ALL-x.y.zzz.tab3.zip elsewhere (file:// for a local "
                    "copy); its name states the version")

    ia = sub.add_parser("ingest-intact", help="land IntAct's MITAB 2.7 export whole, then derive")
    ia.add_argument("--release", required=True, help="biocOnIce release, e.g. 2026.09")
    ia.add_argument("--intact-release", help="IntAct dated release, e.g. 2026-01-09 (default: newest on the FTP)")
    ia.add_argument("--url", help="an intact.zip elsewhere (file:// for a local copy) under a "
                    "<YYYY-MM-DD>/psimitab/ path; the dated directory states the version")

    sub.add_parser("tables", help="list catalog tables")
    args = p.parse_args()

    cat = catalog()
    if args.cmd == "ingest-ensembl":
        if args.transform_only:
            info = ensembl.species_info(args.ensembl_release, args.species)
            counts = ensembl.transform(cat, args.release, info, args.ensembl_release)
        else:
            counts = ensembl.ingest(cat, args.release, args.species, args.ensembl_release)
        _print(counts)
    elif args.cmd == "ingest-bugsigdb":
        n = bugsigdb.land_raw(cat, args.release, args.version)
        print(f"{'raw.bugsigdb__full_dump':40} {n:>10,} rows  ({args.version})")
    elif args.cmd == "ingest-icite":
        _print(icite.ingest(cat, args.release, args.snapshot, args.csv))
    elif args.cmd == "ingest-pubtator3":
        _print(pubtator3.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-cellxgene":
        _print(cellxgene.ingest(cat, args.release, json_path=args.json,
                                census_release=args.census_release))
    elif args.cmd == "ingest-obo":
        _print(obo.ingest(cat, args.release, args.ontology, args.url))
    elif args.cmd == "ingest-bedbase":
        _print(bedbase.ingest(cat, args.release))
    elif args.cmd == "ingest-hgnc":
        _print(hgnc.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-mane":
        _print(mane.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-cellosaurus":
        _print(cellosaurus.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-wikipathways":
        _print(wikipathways.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-eqtlcatalogue":
        _print(eqtlcatalogue.ingest(cat, args.release, args.url, args.eqtl_release))
    elif args.cmd == "ingest-gwas-catalog":
        _print(gwas_catalog.ingest(cat, args.release, args.url))
    elif args.cmd == "ingest-encode":
        _print(encode.ingest(cat, args.release, args.experiments, args.files))
    elif args.cmd == "ingest-rnacentral":
        taxa = [int(t) for t in args.taxa.split(",")] if args.taxa else None
        _print(rnacentral.ingest(cat, args.release, taxa, args.rnacentral_release, args.url))
    elif args.cmd == "ingest-biogrid":
        _print(biogrid.ingest(cat, args.release, args.biogrid_release, args.url))
    elif args.cmd == "ingest-intact":
        _print(intact.ingest(cat, args.release, args.intact_release, args.url))
    elif args.cmd == "ingest-complexportal":
        _print(complexportal.ingest(cat, args.release, args.complexportal_release, args.url))
    elif args.cmd in ncbi_cmds:
        module = ncbi_cmds[args.cmd][0]
        taxa = [int(t) for t in args.taxa.split(",")] if args.taxa else None
        _print(module.ingest(cat, args.release, taxa))
    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
