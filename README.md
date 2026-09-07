# Nordlys Invest

Første MVP for analyse av aksjer og fond med minimum én ukes horisont og uten fast øvre grense.

## Kjør lokalt

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```

Bruk Yahoo Finance-symboler, for eksempel `EQNR.OL`, `DNB.OL`, `AAPL` eller `MSFT`.

## Nåværende modell

Poengsummen kombinerer kurs over/under 50-dagers snitt, forholdet mellom 20-, 50- og 200-dagers snitt samt positiv/negativ 1- og 3-måneders avkastning. Dashboardet viser også RSS-nyheter og sentimentscore fra −5 til +5. Sett `OPENAI_API_KEY` som miljøvariabel for AI-sentiment; uten nøkkel brukes lokal reserveanalyse.
