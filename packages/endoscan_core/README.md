# endoscan_core

The framework-free science library for EndoScan. All scientific logic lives
here and is independently tested. The FastAPI service and the Builder Agent
import this package; neither reimplements science.

At **M0** this is an empty, installable skeleton that exposes only
`endoscan_core.__version__`. Subpackages (`ingest`, `transcriptomics`,
`features`, `training`, `inference`, `explain`, `reporting`, `registry`,
`datasets`) are added in later milestones.
