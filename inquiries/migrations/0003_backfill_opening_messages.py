"""
Give every pre-existing `Inquiry` an opening `InquiryMessage`.

From here on, `POST /inquiries` writes the opener itself, in the same
transaction as the inquiry (§4 of the reply-thread proposal): `body =
inquiry.message`, `sender_side = renter`, `created_at = inquiry.created_at`.
This migration does the same, once, for every row that predates that code —
so "a thread always has at least one message" holds everywhere, not only for
inquiries created after this ships, and the frontend never has to special-case
an old inquiry with an empty transcript.

Filtered on `messages__isnull=True` rather than "every inquiry", so re-running
this (a second `migrate` after a partial run, or a fixture loaded between
`0002` and `0003`) creates no duplicate opener.

Irreversible: an honest reverse cannot tell a backfilled opener from a real
first message written since, and unapplying `0002` drops the table these rows
live in anyway.
"""

from django.db import migrations


def backfill_opening_messages(apps, schema_editor):
    Inquiry = apps.get_model("inquiries", "Inquiry")
    InquiryMessage = apps.get_model("inquiries", "InquiryMessage")

    openers = (
        InquiryMessage(
            inquiry_id=inquiry.pk,
            sender_side="renter",
            created_by_id=inquiry.created_by_id,
            body=inquiry.message,
            created_at=inquiry.created_at,
        )
        for inquiry in Inquiry.objects.filter(messages__isnull=True).iterator()
    )
    InquiryMessage.objects.bulk_create(openers, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("inquiries", "0002_inquirymessage"),
    ]

    operations = [
        migrations.RunPython(
            backfill_opening_messages, migrations.RunPython.noop
        ),
    ]
