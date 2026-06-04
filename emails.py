"""
emails.py — Email transazionali centralizzate (Resend)
"""

import os
import resend
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@repliq.it")
APP_URL = os.getenv("APP_URL", "https://app.repliq.it")


def _base_template(content: str) -> str:
    """Wrapper HTML comune per tutte le email."""
    return f"""
    <!DOCTYPE html>
    <html lang="it">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0; padding:0; background:#f8fafc; font-family: system-ui, -apple-system, sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc; padding: 40px 20px;">
        <tr>
          <td align="center">
            <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">
              <!-- Header -->
              <tr>
                <td style="background: linear-gradient(135deg, #2563eb, #7c3aed); border-radius: 16px 16px 0 0; padding: 32px 40px; text-align: center;">
                  <span style="font-size: 28px; font-weight: 900; color: white; letter-spacing: -0.5px;">Repliq</span>
                  <p style="color: rgba(255,255,255,0.75); margin: 6px 0 0; font-size: 13px;">Il chatbot AI per eventi sportivi</p>
                </td>
              </tr>
              <!-- Content -->
              <tr>
                <td style="background: white; padding: 40px; border-radius: 0 0 16px 16px; border: 1px solid #e2e8f0; border-top: none;">
                  {content}
                  <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 32px 0;">
                  <p style="color: #94a3b8; font-size: 12px; margin: 0; text-align: center;">
                    © 2025 Repliq · <a href="https://repliq.it" style="color: #94a3b8;">repliq.it</a>
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


def send_welcome_email(email: str, name: str) -> None:
    """Email inviata subito dopo la registrazione (account in attesa di approvazione)."""
    content = f"""
    <h2 style="color: #1e293b; margin: 0 0 8px; font-size: 22px;">Benvenuto su Repliq, {name}! 👋</h2>
    <p style="color: #64748b; font-size: 15px; line-height: 1.6; margin: 0 0 24px;">
      La tua registrazione è andata a buon fine. Il tuo account è attualmente <strong style="color: #f59e0b;">in attesa di approvazione</strong> — ti invieremo un'email non appena sarà attivo, di solito entro poche ore.
    </p>

    <div style="background: #f0f7ff; border: 1px solid #bfdbfe; border-radius: 12px; padding: 20px; margin: 0 0 28px;">
      <p style="color: #1d4ed8; font-size: 14px; font-weight: 700; margin: 0 0 12px;">🚀 Nel frattempo, prepara i tuoi contenuti</p>
      <ul style="color: #1e40af; font-size: 14px; line-height: 1.8; margin: 0; padding-left: 20px;">
        <li>PDF del regolamento della tua gara</li>
        <li>File GPX del percorso</li>
        <li>Posizioni parcheggi, partenza, ristori</li>
      </ul>
    </div>

    <p style="color: #64748b; font-size: 14px; line-height: 1.6; margin: 0 0 8px;">
      Hai domande? Rispondi direttamente a questa email.
    </p>
    <p style="color: #64748b; font-size: 14px; margin: 0;">
      — Il team di Repliq
    </p>
    """
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [email],
            "subject": "Benvenuto su Repliq — account in attesa di approvazione",
            "html": _base_template(content),
        })
    except Exception as e:
        print(f"[emails] Errore send_welcome_email: {e}")


def send_approval_email(email: str, name: str, plan: str = "single") -> None:
    """Email inviata quando l'admin approva l'account."""
    plan_labels = {
        "single": "Gara Singola",
        "base": "Stagione Base",
        "pro": "Stagione Pro",
        "federation": "Federazione",
    }
    plan_label = plan_labels.get(plan, plan.capitalize())

    content = f"""
    <h2 style="color: #1e293b; margin: 0 0 8px; font-size: 22px;">Account approvato! 🎉</h2>
    <p style="color: #64748b; font-size: 15px; line-height: 1.6; margin: 0 0 24px;">
      Ciao <strong>{name}</strong>, il tuo account Repliq è stato approvato ed è ora <strong style="color: #16a34a;">attivo</strong>.
      Piano attivato: <strong>{plan_label}</strong>.
    </p>

    <div style="text-align: center; margin: 0 0 32px;">
      <a href="{APP_URL}/login"
         style="display: inline-block; background: linear-gradient(135deg, #2563eb, #7c3aed); color: white;
                padding: 14px 32px; border-radius: 50px; font-weight: 700; font-size: 15px;
                text-decoration: none; letter-spacing: -0.2px;">
        Accedi alla dashboard →
      </a>
    </div>

    <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 20px; margin: 0 0 28px;">
      <p style="color: #15803d; font-size: 14px; font-weight: 700; margin: 0 0 12px;">✅ Come iniziare in 5 minuti</p>
      <ol style="color: #166534; font-size: 14px; line-height: 2; margin: 0; padding-left: 20px;">
        <li>Accedi e crea la tua prima gara</li>
        <li>Carica il PDF del regolamento</li>
        <li>Aggiungi percorso GPX e posizioni logistiche</li>
        <li>Condividi il link o il QR code con i partecipanti</li>
      </ol>
    </div>

    <p style="color: #64748b; font-size: 14px; line-height: 1.6; margin: 0 0 8px;">
      Hai bisogno di aiuto? Rispondi a questa email — siamo qui.
    </p>
    <p style="color: #64748b; font-size: 14px; margin: 0;">
      — Il team di Repliq
    </p>
    """
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [email],
            "subject": "Account Repliq approvato — puoi iniziare! 🎉",
            "html": _base_template(content),
        })
    except Exception as e:
        print(f"[emails] Errore send_approval_email: {e}")


def notify_organizer_ticket(organizer_email: str, organizer_name: str, ticket: dict) -> None:
    """Email all'organizzatore per un nuovo ticket (domanda senza risposta)."""
    content = f"""
    <h2 style="color: #1e293b; margin: 0 0 8px; font-size: 22px;">Nuovo ticket da rispondere 📬</h2>
    <p style="color: #64748b; font-size: 15px; line-height: 1.6; margin: 0 0 24px;">
      Ciao <strong>{organizer_name}</strong>, un partecipante ha posto una domanda a cui il chatbot non ha saputo rispondere.
    </p>

    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px; margin: 0 0 28px;">
      <p style="color: #475569; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 8px;">Gara</p>
      <p style="color: #1e293b; font-size: 15px; font-weight: 600; margin: 0 0 16px;">{ticket.get('race_name', '—')}</p>
      <p style="color: #475569; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 8px;">Domanda</p>
      <p style="color: #1e293b; font-size: 15px; margin: 0; font-style: italic;">"{ticket.get('question', '—')}"</p>
    </div>

    <div style="text-align: center; margin: 0 0 24px;">
      <a href="{APP_URL}/dashboard/tickets"
         style="display: inline-block; background: linear-gradient(135deg, #2563eb, #7c3aed); color: white;
                padding: 14px 32px; border-radius: 50px; font-weight: 700; font-size: 15px;
                text-decoration: none;">
        Rispondi al ticket →
      </a>
    </div>

    <p style="color: #94a3b8; font-size: 13px; margin: 0; text-align: center;">
      Il partecipante riceverà la tua risposta via email in automatico.
    </p>
    """
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [organizer_email],
            "subject": f"Nuovo ticket — {ticket.get('race_name', 'Gara')}",
            "html": _base_template(content),
        })
    except Exception as e:
        print(f"[emails] Errore notify_organizer_ticket: {e}")


def notify_participant_reply(participant_email: str, race_name: str, question: str, reply: str) -> None:
    """Email al partecipante con la risposta dell'organizzatore."""
    content = f"""
    <h2 style="color: #1e293b; margin: 0 0 8px; font-size: 22px;">Risposta alla tua domanda ✅</h2>
    <p style="color: #64748b; font-size: 15px; line-height: 1.6; margin: 0 0 24px;">
      Lo staff di <strong>{race_name}</strong> ha risposto alla tua domanda.
    </p>

    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px; margin: 0 0 16px;">
      <p style="color: #475569; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 8px;">La tua domanda</p>
      <p style="color: #64748b; font-size: 14px; font-style: italic; margin: 0;">"{question}"</p>
    </div>

    <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 20px; margin: 0 0 28px;">
      <p style="color: #475569; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 8px;">Risposta</p>
      <p style="color: #166534; font-size: 15px; line-height: 1.7; margin: 0;">{reply}</p>
    </div>

    <p style="color: #94a3b8; font-size: 13px; margin: 0; text-align: center;">
      — Lo staff di {race_name}
    </p>
    """
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [participant_email],
            "subject": f"Risposta alla tua domanda — {race_name}",
            "html": _base_template(content),
        })
    except Exception as e:
        print(f"[emails] Errore notify_participant_reply: {e}")
