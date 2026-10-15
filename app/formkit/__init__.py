"""Űrlapok bármiből, aláírás, ügynöki kitöltés.

    schema     egységes mezőséma (minden formátumra ugyanaz)
    detect     felismerés: kitölthető és lapos PDF, DOCX, XLSX, fotó/szken → séma
    fill       kitöltés az eredeti formátumban (PDF-mezők, lapos PDF ráírással, DOCX, XLSX, fotó eredeti kinézettel)
    validate   dátum, IBAN, magyar azonosítók (adóazonosító jel, TAJ, adószám, bankszámlaszám) ellenőrzőszámmal
    signature  aláírás/kézjegy tisztítása átlátszó PNG-vé, titkosított tárolás, elhelyezés PDF-be és DOCX-be
    profile    felhasználói adatprofil (titkosítva) és a mezők automatikus megfeleltetése
    crypto     AES-256-GCM felhasználónként külön kulccsal (HKDF)
"""
