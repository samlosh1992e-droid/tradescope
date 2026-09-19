# TradeScope

Service web : l'utilisateur envoie un screenshot de son graphique
(BTC/USD, Or, EUR/USD...) ; le site analyse le marche en temps reel
et renvoie un plan d'action : entree, stop loss, TP1/TP2/TP3 et le
raisonnement. Modele freemium : 3 analyses gratuites par email, puis
abonnement mensuel.

## Fonctionnalites

- Upload de screenshot (PNG/JPG/WEBP) + choix marche / unite de temps
- Donnees de marche reelles (Yahoo Finance) pour le symbole choisi
- Indicateurs : EMA 9/21/50, RSI(14), ATR(14), structure (plus hauts/bas)
- Plan : biais, entree, SL, TP1 (1R), TP2 (2R), TP3 (3R), risque recommande 1-2 %
- Raisonnement explique en toutes lettres
- Graphique annote (matplotlib) + niveaux dessines sur le screenshot (PIL)
- Freemium : 3 credits offerts, page d'abonnement, espace pro pour crediter
- Avertissements legaux (outil educatif, risque de perte)

## Lancement

```bash
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

## Variables d'environnement

- SECRET_KEY : secret Flask
- ADMIN_TOKEN : code de l'espace pro (defaut tradescope2026)
- PAY_LINK : lien de paiement de l'abonnement (Stripe ou autre)
- FREE_CREDITS : nombre d'analyses offertes (defaut 3)

## Espace pro

URL /admin + code secret. Permet de voir les utilisateurs, leurs credits
restants et les 30 dernieres analyses, et d'ajouter des credits
(utile quand un abonne a paye sa facture).

## Points a verifier avant de vendre

- Le service est un outil d'analyse, PAS une promesse de gains. Garder
  les avertissements affiches.
- Vendre un service de conseil financier est reglemente (AMF/ESMA).
  Ce produit reste un outil educatif : ne jamais garantir un rendement.
- Encaisssement : definir PAY_LINK (ex : Stripe Payment Link) puis
  crediter les emails payants via l'espace pro.

## Tests

python test_app.py  (recree la base a chaque run)