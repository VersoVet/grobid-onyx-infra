# Grobid Onyx Infrastructure - Guide Utilisateur

## Description

Service d'extraction de metadonnees et de texte structure depuis des documents PDF scientifiques. Utilise Grobid avec des modeles Deep Learning optimises pour les publications academiques.

## Documentation

- **Technique**: Voir [ARCHITECTURE.md](./ARCHITECTURE.md)
- **Forge/Dev**: Voir [/opt/onyx/forge/CLAUDE.md](/opt/onyx/forge/CLAUDE.md)
- **Infra Onyx**: Voir [/opt/onyx/docs/](/opt/onyx/docs/)
- **Grobid officiel**: https://grobid.readthedocs.io/

## Acces

| Service | URL |
|---------|-----|
| Wrapper API | http://10.0.0.21:8071 |
| Grobid direct | http://10.0.0.21:8070 |
| Via Soma (proxy) | http://10.0.0.44:8070 |
| Documentation | http://10.0.0.21:8071/docs |

## Endpoints principaux

| Endpoint | Methode | Description |
|----------|---------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Statut detaille |
| `/api/processFulltextDocument` | POST | Extraction PDF complete → TEI XML |
| `/api/processHeaderDocument` | POST | Extraction metadonnees uniquement |
| `/api/processReferences` | POST | Extraction references bibliographiques |

## Utilisation

### Extraction complete d'un PDF

```bash
curl -X POST http://10.0.0.21:8071/api/processFulltextDocument \
  -F "input=@article.pdf" \
  -F "consolidateHeader=1" \
  -o result.xml
```

### Extraction metadonnees uniquement

```bash
curl -X POST http://10.0.0.21:8071/api/processHeaderDocument \
  -F "input=@article.pdf" \
  -o header.xml
```

### Via Python

```python
import httpx

with open("article.pdf", "rb") as f:
    response = httpx.post(
        "http://10.0.0.21:8071/api/processFulltextDocument",
        files={"input": f},
        data={"consolidateHeader": 1}
    )
    tei_xml = response.text
```

## Configuration

- **Concurrence**: 28 workers paralleles
- **Memoire JVM**: 48 Go max
- **Timeout PDF**: 180 secondes
- **Stockage partage**: `/mnt/sandbox/grobid-share` (NFS depuis soma)

## Maintenance

```bash
# Verifier le statut
curl http://10.0.0.21:8071/status

# Redemarrer les containers
curl -X POST http://10.0.0.21:8071/docker/restart

# Voir les logs
curl http://10.0.0.21:8071/docker/logs?lines=50
```
