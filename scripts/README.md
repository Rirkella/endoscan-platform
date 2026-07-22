# Maintained repository scripts

The canonical developer interface is `python scripts/project.py <task>`; see
[Development](../docs/DEVELOPMENT.md). The remaining scripts have narrow maintained roles:

| Script | Role |
|---|---|
| `project.py` | Cross-platform setup, test, build, demo, and verification tasks |
| `verify_repository.py` | Offline documentation, layout, serialization, secret, and artifact checks |
| `run_offline_endpoint_demo.py` | Complete deterministic TR-like or DNA-damage lifecycle fixture |
| `extract_gene_annotations.py` | Maintainer utility for regenerating reviewed frontend gene annotations |
| `extract_reactome_pathways.py` | Maintainer utility for regenerating the reviewed Reactome reference artifact |

The two extraction utilities require separately obtained source inputs. They are not part of
the default setup, CI, or offline demo, and they do not perform implicit network requests.
