-- Migrazione: flag abilitazione "Importa da URL" per organizzatore
-- Aggiunge il campo che controlla se un organizzatore può usare l'import gara da URL.
-- Da eseguire nel SQL Editor di Supabase PRIMA di deployare il backend aggiornato
-- (altrimenti /api/import-from-url e il pannello admin vanno in errore).
--
-- DEFAULT false => TUTTI gli account (esistenti e nuovi) partono DISABILITATI.
-- L'abilitazione si fa manualmente dal pannello admin (pulsante "🔗 URL ON/OFF").

ALTER TABLE organizers
  ADD COLUMN IF NOT EXISTS url_import_enabled boolean NOT NULL DEFAULT false;
