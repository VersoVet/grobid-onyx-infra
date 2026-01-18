# Grobid Onyx Infrastructure - Architecture

## Vue d'ensemble

Ce skill encapsule [Grobid](https://github.com/kermitt2/grobid), un service d'extraction de metadonnees et de texte structure depuis des documents PDF scientifiques.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         OnyxAxon (10.0.0.21)                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────┐      ┌──────────────────────────────────┐ │
│  │   Python Wrapper    │      │      Grobid Container            │ │
│  │   (FastAPI)         │      │      (grobid/grobid:0.8.1)       │ │
│  │                     │      │                                  │ │
│  │   Port: 8071        │─────▶│   Port: 8070                     │ │
│  │                     │      │                                  │ │
│  │   - Health check    │      │   - Deep Learning models         │ │
│  │   - Proxy API       │      │   - CRF models                   │ │
│  │   - Docker mgmt     │      │   - PDF parsing (pdfalto)        │ │
│  │   - Onyx SDK        │      │                                  │ │
│  └─────────────────────┘      └──────────────────────────────────┘ │
│            │                              │                         │
│            │                              │                         │
│            ▼                              ▼                         │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │              /mnt/sandbox/grobid-share (NFS)                    ││
│  │              Partage depuis OnyxSoma                            ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
            │
            │ NFS
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         OnyxSoma (10.0.0.44)                        │
├─────────────────────────────────────────────────────────────────────┤
│   /opt/onyx/sandbox/grobid-share/                                   │
│   └── [PDFs a traiter]                                              │
│                                                                     │
│   Redirection proxy: port 8070 ───socat───▶ OnyxAxon:8070          │
└─────────────────────────────────────────────────────────────────────┘
```

## Configuration Optimisee

Le skill est optimise pour OnyxAxon (32 coeurs, 64 Go RAM):

| Parametre | Valeur | Description |
|-----------|--------|-------------|
| concurrency | 28 | Workers paralleles (32 - 4 pour systeme) |
| JVM Xmx | 48g | Memoire max Java |
| GC ParallelThreads | 16 | Threads garbage collector |
| PDF memoryLimitMb | 8192 | Limite memoire PDF |
| PDF timeoutSec | 180 | Timeout parsing PDF |

## Endpoints API

### Wrapper (port 8071)

| Endpoint | Methode | Description |
|----------|---------|-------------|
| `/health` | GET | Health check unifie |
| `/status` | GET | Statut detaille |
| `/docker/restart` | POST | Redemarre les containers |
| `/docker/logs` | GET | Logs des containers |

### Proxy Grobid (via wrapper)

| Endpoint | Methode | Description |
|----------|---------|-------------|
| `/api/isalive` | GET | Grobid alive check |
| `/api/version` | GET | Version Grobid |
| `/api/processFulltextDocument` | POST | Extraction complete PDF |
| `/api/processHeaderDocument` | POST | Extraction metadonnees |
| `/api/processReferences` | POST | Extraction references |
| `/api/processCitation` | POST | Parse citation texte |

### Acces direct Grobid (port 8070)

L'API Grobid complete est accessible sur le port 8070:
- Documentation: http://10.0.0.21:8070/api
- Console: http://10.0.0.21:8070

## Modeles Deep Learning

Grobid utilise des modeles pre-entraines:

| Modele | Engine | Usage |
|--------|--------|-------|
| header | DeLFT BidLSTM | Titre, auteurs, abstract |
| citation | DeLFT BidLSTM | References bibliographiques |
| affiliation-address | DeLFT BidLSTM | Affiliations |
| segmentation | Wapiti CRF | Segmentation document |
| fulltext | Wapiti CRF | Corps du texte |
| figure/table | Wapiti CRF | Figures et tableaux |

## Flux de donnees

```
1. Client ──────────────────────────────────────────────────────────┐
   │                                                                │
   │ POST /api/processFulltextDocument                              │
   │ (PDF file)                                                     │
   ▼                                                                │
2. Wrapper Python (8071) ───────────────────────────────────────────┤
   │ - Validation                                                   │
   │ - Set status "working"                                         │
   │ - Proxy vers Grobid                                            │
   ▼                                                                │
3. Grobid Container (8070) ─────────────────────────────────────────┤
   │ - PDF parsing (pdfalto)                                        │
   │ - Segmentation                                                 │
   │ - Header extraction (DL)                                       │
   │ - Fulltext extraction (CRF)                                    │
   │ - Citation extraction (DL)                                     │
   │ - TEI XML generation                                           │
   ▼                                                                │
4. Response ────────────────────────────────────────────────────────┘
   (TEI XML)
```

## Deploiement

### Prerequis sur OnyxAxon

1. Docker installe
2. Montage NFS: `/mnt/sandbox` depuis soma

### Commandes

```bash
# Deploiement via Forge
forge deploy grobid-onyx-infra

# Demarrage manuel
cd /opt/onyx/skills/grobid-onyx-infra
docker compose -f docker/docker-compose.yml up -d
python -m src.main
```

## Dependances

- **Docker**: grobid/grobid:0.8.1
- **Python**: FastAPI, httpx, docker SDK
- **NFS**: Partage soma:/opt/onyx/sandbox

## Securite

- Pas de credentials requis
- Acces reseau interne uniquement (10.0.0.0/24)
- CORS configure pour tous les origines (API interne)
