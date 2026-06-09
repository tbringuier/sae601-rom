# ClimaCampus — SAÉ601-ROM

Réseau météo participatif universitaire basé sur des stations **LoRaWAN**.
Une station mesure CO₂, température, humidité et pression ; les données arrivent
via [The Things Network](https://www.thethingsnetwork.org/) (MQTT), sont stockées
en SQLite et visualisées sur un tableau de bord temps réel avec **détection
d'anomalie climatique** (comparaison aux normales Météo-France).

🌍 **Démo en ligne :** https://tbringuier.github.io/sae601-rom/

Projet réalisé en BUT3 R&T ROM à l'IUT de Villetaneuse par
Tristan Bringuier, Irsa Ahmad, Nolan Heu--Combe et Thierry Banoho.

---

## Lancer l'application (Docker)

1. Copier le modèle de configuration et y mettre vos accès MQTT (TTN) :

   ```bash
   cp feeds.example.json feeds.json
   # éditer feeds.json : pour chaque université, host/user/password TTN
   ```

2. Démarrer :

   ```bash
   docker compose up -d
   ```

3. Ouvrir http://localhost:5000

La base de données et les sauvegardes sont persistées dans le volume `/data` ;
`feeds.json` est monté en lecture seule dans le conteneur.

## Configuration des flux (`feeds.json`)

Chaque université déclare ses coordonnées et un flux MQTT TTN. L'application
lance un abonnement par flux et rattache les capteurs reçus à leur université
(via `applications`). `device_labels` permet d'afficher un nom lisible
(ex. `R201`). Mettre `feed.enabled` à `false` pour une université sans collecte.

## Site statique (GitHub Pages)

Le dossier `dist/` est une version 100 % statique (démonstration), publiée sur
la branche `pages`. Pour la régénérer :

```bash
python3 build_static.py          # exporte les données réelles + sites de démo
# puis copier dist/ dans la branche pages et pousser
```

## Algorithme de détection d'anomalie

Pour chaque jour, la température moyenne mesurée est comparée à la **normale
climatique mensuelle nationale** (Météo-France, normales 1991–2020). L'écart est
normalisé par l'écart-type saisonnier σ pour obtenir un z-score
`z = (T_mesurée − T_normale) / σ`, classé en *normal* (`|z| < 1`),
*anomalie modérée* (`1 ≤ |z| < 2`) ou *anomalie forte* (`|z| ≥ 2`).

> Le capteur étant en intérieur, l'écart absolu aux normales extérieures est
> attendu : l'algorithme met en avant la **variation relative** dans le temps.

Les universités Madrid, Berlin, Milan et Lisbonne de la démo sont **simulées**.
