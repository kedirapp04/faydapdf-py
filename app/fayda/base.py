"""Provider interface + result shapes shared by both Fayda modes.

Flow:
  send_otp(individual_id)  -> {ok, session, masked_mobile} | {ok:False, error}
  verify_pdf(session, otp) -> {ok, pdf, filename}          | {ok:False, error}
  forgot_fan(name, phone)  -> {ok, phone, message}         | {ok:False, error}

`session` is an opaque token the provider hands back from send_otp and consumes in
verify_pdf (a fayda-railway sessionId in API mode; the internal auth session in
Server-4 mode).
"""


class FaydaProvider:
    name = "base"

    async def send_otp(self, individual_id: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def verify_pdf(self, session, otp: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def forgot_fan(self, name: str, phone: str) -> dict:  # pragma: no cover
        raise NotImplementedError


def ok(**kw) -> dict:
    d = {"ok": True}
    d.update(kw)
    return d


def err(message: str, **kw) -> dict:
    d = {"ok": False, "error": message}
    d.update(kw)
    return d
