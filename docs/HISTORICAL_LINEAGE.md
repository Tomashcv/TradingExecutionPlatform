# Historical lineage

The source tree began as an internal `SP1Execution` project and was later extended with an additive recovery execution track.

The later snapshot is a strict source superset of the earlier execution snapshot: the original files are preserved while recovery contracts, source modules and tests are added. The public portfolio release therefore uses the later snapshot as its code base instead of presenting two nearly identical repositories.

The historical `sp1execution` Python namespace, CLI name and contract identifiers are retained. They are provenance identifiers, not the public product name.

Original freeze notes are retained under `docs/history/` as historical engineering records.
