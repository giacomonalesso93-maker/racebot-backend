-- Migrazione: Regolamento Master Evento
-- Aggiunge il campo regolamento master a livello evento, valido per tutte le gare.
-- Da eseguire nel SQL Editor di Supabase PRIMA di deployare il backend aggiornato
-- (altrimenti /api/ask va in errore sul SELECT che ora include text_regulation).

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS text_regulation text;

-- Nessun valore di default: NULL = nessun regolamento master impostato.
-- Il campo NON viene mostrato nella pagina pubblica, serve solo al chatbot.
