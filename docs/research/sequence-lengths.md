# Where sequence lengths and circularity come from

**Status**: research note, not a decision. Written 2026-09-18 for issue #6;
feeds #42 (assembly reports) and #94 (assembly scoping). Every observation below
was made against the live source on that date, Ensembl release 116.

## The question

`reference.sequence` needs a length and an `is_circular` flag per sequence, per
species and Ensembl release, so clients can rebuild `seqinfo()` and clamp
`promoters()` at sequence ends. A GTF carries neither. Four candidates were named.

## What each candidate actually gives

| Candidate | Lengths | Circularity | Per release | Cost | Coverage |
|---|---|---|---|---|---|
| FASTA index `.fai` | every top-level sequence | no | yes, under `release-N/` | 27 KB for human (706 sequences) | all 359 species directories on the main FTP |
| GFF3 `##sequence-region` + region features | every sequence that has a region feature | `Is_circular=true`, **unreliable** | yes | the whole GFF3: 108 MB for human | same species as the GTF |
| Ensembl REST `/info/assembly/:species` | every top-level sequence | per-sequence call, **unreliable** | **no** — current release only | 1 call + 1 per sequence, rate-limited | current release only |
| NCBI assembly report | every sequence in the assembly | no column; molecule *type* instead | per assembly version, not per Ensembl release | ~100 KB | needs the assembly accession |

**`.fai`.** Ensembl publishes a bgzipped top-level FASTA with a samtools index at
`https://ftp.ensembl.org/pub/release-{N}/fasta/{species}/dna_index/{Species}.{assembly}.dna.toplevel.fa.gz.fai`.
Columns 1–2 are name and length, in the GTF's own sequence names (`1`, `X`,
`MT`, `KI270757.1`), so there is no name mapping to do. Walking
`release-116/fasta/*/dna_index/` found a `.fai` for every directory except
`ancestral_alleles`, which is not a species. It says nothing about circularity.

**GFF3.** `##sequence-region I 1 230218` gives lengths, and the region feature
carries `Is_circular=true` — for yeast `Mito`. But the human and mouse MT
features carry no such attribute:

    MT  GRCh38  chromosome  1  16569  .  .  .  ID=chromosome:MT;Alias=chrM,J01415.2,NC_012920.1
    MT  GRCm39  chromosome  1  16299  .  .  .  ID=chromosome:MT;Alias=NC_005089.1,AY172335.1,chrM

So Ensembl's circular flag is simply not set for the two most-used genomes. The
same region features do carry something useful: `Alias=` lists the UCSC, INSDC
and RefSeq names of each chromosome, in Ensembl's own release, for every species
— most of what #42 wants assembly reports for.

**REST.** `GET /info/assembly/homo_sapiens/MT` returns `"is_circular":0` (mouse
MT too; yeast `Mito` and *E. coli* `Chromosome` return 1). Same underlying flag,
same gap, and REST serves only the current release — it cannot reproduce a past
one, which rules it out on its own.

**Assembly report.** No circularity column. It has
`Assigned-Molecule-Location/Type` (`Chromosome`, `Mitochondrion`, and for other
taxa `Chloroplast`, `Plasmid`…), lengths, and GenBank/RefSeq/UCSC names. Its
names are NCBI's, keyed by assembly accession (Ensembl's GRCh38 is
`GCA_000001405.29`); `reference.genome` has no accession column yet (#94).

## Recommendation

1. **Lengths: the `.fai`.** Complete, per release, tiny, already in the GTF's
   names, one predictable URL per species. It lands whole as
   `raw.ensembl__fai` beside `raw.ensembl__gtf` with the same
   `(taxon_id, ensembl_release)` overwrite scope — and inherits #94's
   one-assembly-per-taxon problem along with it.
2. **Circularity: no source states it reliably, so derive it and say so.**
   Organelle and plasmid sequences are circular; nuclear chromosomes of the
   eukaryotes on this FTP are not. GenomeInfoDb does the same thing with a name
   list. The honest column doc is "inferred from molecule type, not asserted by
   the source". Two ways to get the molecule type:
   - cheap: the name (`MT`, `Mito`, `Pt`, `chrM`…) — exactly GenomeInfoDb's
     heuristic, no new source;
   - better: the assembly report's molecule type, once #94 gives
     `reference.genome` an accession to fetch it by. Take Ensembl's
     `Is_circular=true` as an additional positive signal, never its absence as
     a negative one.
3. **Aliases (#42's other half): the GFF3 region features' `Alias=`** cover
   chromosomes for every Ensembl species without an accession lookup. Assembly
   reports remain the complete answer (scaffolds included) and the only one for
   RefSeq-native work (#44).

**UNVERIFIED**: Ensembl Genomes (plants, fungi, metazoa, bacteria) live on a
different FTP; whether `dna_index/` exists there was not checked. Only the
directories under `release-116/fasta/` on the main FTP were walked.
