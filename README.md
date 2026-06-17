# Migration de données CSV vers MongoDB (Docker/Compose + Reporting, AWS-ready)

Ce dépôt contient un script Python qui migre un dataset de santé (CSV) vers MongoDB.
Il applique des **transformations contrôlées**, crée un **index composé unique** pour assurer l'**idempotence**, propose un mode **upsert** pour réexécuter la migration sans doublons, et génère un **rapport d’exécution** persistant.

![Architecture](docs/architecture.png)

---

## 📊 Données

Le jeu de données est le **« Healthcare Dataset »** public de Kaggle : des données **entièrement synthétiques** (noms et valeurs fictifs), sans aucune PII réelle — utilisées ici uniquement à des fins de démonstration technique.

Un **échantillon** de quelques lignes est versionné dans `data/healthcare_dataset_subset.csv` pour permettre de lancer le projet immédiatement. Le dataset complet (~55 500 lignes) est disponible sur Kaggle ; pour l'utiliser, placez-le dans `data/` et pointez `CSV_FILENAME` dessus dans `.env`.

---

## 🎯 Objectif

* Charger un CSV (potentiellement volumineux) dans MongoDB de manière **reproductible**.
* **Nettoyer/normaliser** certaines colonnes et **typer** les champs.
* Empêcher les doublons grâce à une **clé naturelle** + **index unique** + **upsert**.
* **Tracer les exécutions** dans un `report.txt` (lignes du CSV, doublons, lignes incomplètes, documents écrits).

---

## 🧱 Schéma logique (document MongoDB)

| Champ (Mongo)        | Source CSV           | Type cible       | Règle de transformation                         |
| -------------------- | -------------------- | ---------------- | ----------------------------------------------- |
| `name`               | `Name`               | string           | `lower()`                                       |
| `age`                | `Age`                | int              | coercition → int                                |
| `gender`             | `Gender`             | string           | `lower()`                                       |
| `blood_type`         | `Blood Type`         | string           | `lower()`                                       |
| `medical_condition`  | `Medical Condition`  | string           | trim                                            |
| `date_of_admission`  | `Date of Admission`  | datetime (00:00) | parse `dayfirst=True` + normalisation date-only |
| `doctor`             | `Doctor`             | string           | trim                                            |
| `hospital`           | `Hospital`           | string           | `lower()`                                       |
| `insurance_provider` | `Insurance Provider` | string           | trim                                            |
| `billing_amount`     | `Billing Amount`     | float            | coercition → float                              |
| `room_number`        | `Room Number`        | string           | conserver tel quel                              |
| `admission_type`     | `Admission Type`     | string           | trim                                            |
| `discharge_date`     | `Discharge Date`     | datetime (00:00) | parse + normalisation                           |
| `medication`         | `Medication`         | string           | trim                                            |
| `test_results`       | `Test Results`       | string           | trim                                            |
| `ingested_at`        | —                    | datetime (UTC)   | défini à l’insertion                            |
| `last_modified_at`   | —                    | datetime (UTC)   | défini à chaque upsert                          |
| `source`             | —                    | string           | `csv_migration_v2`                              |

> Colonnes CSV attendues (sensible à la casse) :
> `['Name','Age','Gender','Blood Type','Medical Condition','Date of Admission','Doctor','Hospital','Insurance Provider','Billing Amount','Room Number','Admission Type','Discharge Date','Medication','Test Results']`

---

## 🔑 Clé naturelle & index

* **Clé naturelle** choisie : (`name`, `gender`, `blood_type`, `date_of_admission`, `hospital`).
* Tous les champs de la clé sont **normalisés** (lowercase) et la date est **au jour** (00:00:00).
* **Index composé unique** pour empêcher les doublons :

```python
collection.create_index(
    [("name", 1), ("gender", 1), ("blood_type", 1), ("date_of_admission", 1), ("hospital", 1)],
    unique=True,
    name="uniq_admission",
)
```

**Pourquoi un index ?**

* Accélère les recherches (évite le scan complet).
* **Garantit l’unicité** des admissions côté base. Sans index unique, une réexécution pourrait insérer des doublons.

---

## ⚙️ Fonctionnement du script

Script principal : **`app/migrate_to_mongo.py`**

* Lecture streaming du CSV en **chunks** (`--chunksize`), transformation ligne par ligne.
* **Dry-run** pour prévisualiser sans écrire en base.
* **Upsert** par défaut : réexécutions idempotentes (mise à jour si déjà présent, insertion sinon).
* **Index** : `--create-indexes` crée l’index unique (à lancer au moins une fois).
* **Reporting** : à la fin de chaque exécution (hors `--dry-run`), appende une ligne à `report.txt` avec :

  * `total_rows` (lignes du CSV), `duplicates_in_csv` (doublons trouvés via la clé naturelle **dans le CSV**),
  * `missing_key_rows` (lignes sans clé naturelle complète), `upserted_or_modified` (documents insérés/modifiés).

Exemple de ligne dans le rapport :

```
[2026-01-18T15:42:10Z] csv=healthcare_dataset.csv total_rows=1000 duplicates_in_csv=7 missing_key_rows=2 upserted_or_modified=991
```

---

## 📦 Prérequis & installation

* Python 3.10+
* MongoDB local ou distant
* Installation des dépendances Python (exécution locale hors Docker) :

```bash
pip install -r requirements.txt
```

> Variante conda : `conda install pandas pymongo` puis `pip install python-dotenv`.

---

## ▶️ Exemples d’exécution (local)

1. **Prévisualisation (dry-run)**

```bash
python app/migrate_to_mongo.py --csv ./data/healthcare_dataset_subset.csv --dry-run
```

2. **Création de l’index + upsert**

```bash
python app/migrate_to_mongo.py \
  --csv ./data/healthcare_dataset_subset.csv \
  --mongo-uri "mongodb://localhost:27017" \
  --db healthcare --collection patients \
  --create-indexes --upsert
```

3. **Réexécuter en toute sécurité (idempotent)**

```bash
python app/migrate_to_mongo.py --csv ./data/healthcare_dataset_subset.csv --upsert
```

4. **Insérer sans upsert (non recommandé ici)**

```bash
python app/migrate_to_mongo.py --csv ./data/healthcare_dataset_subset.csv --no-upsert
```

---

## 🐳 Docker & Docker Compose (solution complète + permissions minimales)

### 🔐 Authentification & rôles utilisateurs (RBAC)

Cette solution utilise l’authentification native MongoDB (username/password) et une séparation claire des comptes :

1. **Compte “root” (administration)**  
   Créé automatiquement au *premier démarrage* par l’image officielle MongoDB via :
   - `MONGO_INITDB_ROOT_USERNAME`
   - `MONGO_INITDB_ROOT_PASSWORD`  
   Ce compte sert uniquement aux opérations d’administration (debug, maintenance). **Il n’est pas utilisé par le script de migration.**

2. **Compte “app” (migration / application) — *least privilege***  
   Créé au *premier démarrage* par le script `docker/mongo-init/001-create-app-user.sh` dans la base `MONGO_DB`.  
   Ce compte est celui utilisé par le conteneur `loader` pour exécuter la migration.

#### Rôles attribués au compte applicatif

Le compte `MONGO_APP_USER` reçoit (sur la base `MONGO_DB`) :

- `readWrite` : permet d’insérer et de mettre à jour des documents (nécessaire au mode **upsert**)
- `dbAdmin` : permet la gestion des index (nécessaire à `--create-indexes`)

> Variante plus stricte (optionnelle) : créer un rôle custom limité (ex. `createIndex` + opérations d’écriture) au lieu de `dbAdmin`.  
> Pour un projet pédagogique, `dbAdmin` **uniquement sur la base applicative** reste acceptable et simple à expliquer.

#### Chaîne de connexion utilisée par le loader

Dans `docker-compose.yml`, le loader se connecte à MongoDB via le nom de service Docker `mongo` (DNS interne Compose) :

```
mongodb://${MONGO_APP_USER}:${MONGO_APP_PASSWORD}@mongo:27017/${MONGO_DB}?authSource=${MONGO_DB}
```

- `authSource=${MONGO_DB}` : indique à MongoDB où authentifier l’utilisateur (la base applicative).
- On évite d’utiliser le compte root afin de respecter le principe du **moindre privilège**.

#### Où sont stockés les secrets ?

- Les variables sont centralisées dans `.env` (pour l’exécution locale **et** via Docker Compose).
- Recommandation Git : versionner un `.env.example` et ignorer `.env` (car contient des secrets).

#### Vérification rapide (démonstration)

Se connecter en root (admin) :
```bash
docker exec -it mongo mongosh -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" --authenticationDatabase admin
```

Lister les utilisateurs de la base applicative :
```javascript
use healthcare
db.getUsers()
```

Se connecter avec l’utilisateur applicatif :
```bash
docker exec -it mongo mongosh -u "$MONGO_APP_USER" -p "$MONGO_APP_PASSWORD" --authenticationDatabase "$MONGO_DB" "$MONGO_DB"
```

### Arborescence

```
csv-to-mongodb-migration/
├─ .env.example                   # modèle de configuration (copier en .env)
├─ .gitignore
├─ docker-compose.yml
├─ data/                          # vos CSV (montés en lecture seule)
│  └─ healthcare_dataset_subset.csv
├─ docs/
│  └─ architecture.png
├─ docker/
│  └─ mongo-init/
│     └─ 001-create-app-user.sh   # création utilisateur applicatif (RBAC)
└─ app/
   ├─ Dockerfile                  # image du loader (non-root)
   ├─ requirements.txt
   └─ migrate_to_mongo.py
```

### Choix d’architecture

* **mongo** : base MongoDB (image officielle), volume nommé `mongo_data` pour la persistance.
* **reports-init** : conteneur éphémère (busybox) qui **chown** le volume nommé `reports_data` vers l’UID/GID de l’utilisateur applicatif (ex. 10001:10001). Il s’exécute **une seule fois** au démarrage.
* **loader** : conteneur Python **non-root** (UID/GID fixés) qui lit les CSV en bind-mount **en lecture seule** et écrit le rapport dans le volume nommé (`/reports/report.txt`).

### Pourquoi ce design ?

* **Permissions minimales** : le process applicatif n’a pas de privilèges root et n’écrit pas sur l’hôte.
* **Lisibilité & portabilité** : les CSV restent accessibles côté hôte, le rapport persiste dans un volume Docker.
* **Séparation des rôles** : un init minimal fait l’opération d’ownership une fois, le loader reste non-root.

### Lancement

```bash
docker compose build
docker compose up
```

* Au **premier démarrage** :

  * `mongo` initialise l’admin root et lance le script d’init pour créer l’utilisateur applicatif (RBAC).
  * `reports-init` prépare le volume `reports_data` (ownership 10001:10001).
  * `loader` attend que Mongo soit **healthy**, crée l’index si demandé, charge les données et écrit dans le rapport.

* **Réexécutions** :

  * Relancer `docker compose up` rejoue le chargement en mode **idempotent** (pas de doublons grâce à l’index + upsert).

### Consulter le rapport (one-liner fiable)

Fonctionne même pendant `docker compose up` :

```bash
docker compose run --rm --no-deps reports-init sh -lc 'tail -n 50 /reports/report.txt || echo "No report yet at /reports/report.txt"'
```

### Copier le rapport sur l’hôte (optionnel)

```bash
docker compose run --rm --no-deps -v reports_data:/reports -v "$PWD":/host busybox sh -lc 'cp /reports/report.txt /host/'
```
