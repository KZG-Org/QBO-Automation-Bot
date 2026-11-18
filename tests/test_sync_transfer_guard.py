import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from src.connectors.qbo_client import QBOClient
from src.logic.syncing import QBOSync
from src.logic.transformer import process_transfers


def _sync_with_accounts(accounts: dict) -> QBOSync:
    """QBOSync with a stubbed chart of accounts and a recording client."""
    client = MagicMock()
    client.realm_id = "9341455236413167"
    client.client_name = "KZO"
    client.post.return_value = {"Transfer": {"Id": "1"}}
    with patch.object(QBOSync, "_get_qbo_mappings", return_value={}):
        sync = QBOSync(client)
    sync.mappings = {"accounts": accounts, "accounts_meta": {}}
    return sync


def _transfer_row(from_name: str, to_name: str) -> pd.Series:
    return pd.Series({
        "Ref No": "KZOTH0726T0001",
        "Transfer Funds From": from_name,
        "Transfer Funds To": to_name,
        "Transfer Amount": 100.0,
        "Memo": "KZOTH0726T0001 - moving funds",
        "Date": "2026-07-15",
        "Currency": "USD",
    })


class TransferSameAccountTests(unittest.TestCase):
    def test_same_resolved_account_is_blocked_before_posting(self):
        # The leaf name and the full path are two different sheet names for one account Id --
        # which QBO answers with a bare 400 Bad Request on /transfer.
        sync = _sync_with_accounts({"Bank:KZO CBD Z": "77"})

        with self.assertRaises(ValueError) as ctx:
            sync.push_transfer(_transfer_row("KZO CBD Z", "Bank:KZO CBD Z"))

        self.assertIn("same QBO account", str(ctx.exception))
        self.assertIn("77", str(ctx.exception))
        sync.client.post.assert_not_called()

    def test_identical_names_are_blocked(self):
        sync = _sync_with_accounts({"KZO CBD Z": "77"})

        with self.assertRaises(ValueError):
            sync.push_transfer(_transfer_row("KZO CBD Z", "KZO CBD Z"))

        sync.client.post.assert_not_called()

    def test_distinct_accounts_still_post(self):
        sync = _sync_with_accounts({"KZO CBD Z": "77", "KZO Bank TH": "88"})

        sync.push_transfer(_transfer_row("KZO CBD Z", "KZO Bank TH"))

        path, payload = sync.client.post.call_args[0]
        self.assertTrue(path.endswith("/transfer"))
        self.assertEqual(payload["FromAccountRef"], {"value": "77"})
        self.assertEqual(payload["ToAccountRef"], {"value": "88"})



def _raw_transfer_df(from_name: str, to_name: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "No": 1,
        "Date": "2026-07-15",
        "USD - QBO": 100.0,
        "QBO Method": "Transfer",
        "QBO Transfer Fr": from_name,
        "QBO Transfer To": to_name,
        "Item Description": "moving funds",
        "CO": "",
        "Type": "",
    }])


class TransformSameAccountTests(unittest.TestCase):
    """The same collision, caught one stage earlier so the sheet shows it before syncing."""

    def test_same_resolved_account_is_flagged_in_remarks(self):
        out, _ = process_transfers(
            _raw_transfer_df("KZO CBD Z", "Bank:KZO CBD Z"),
            country="TH",
            start_no=0,
            qbo_mappings={"accounts": {"Bank:KZO CBD Z": "77"}, "locations": {}},
            client_name="KZO",
        )

        remark = out["Remarks"].iloc[0]
        self.assertIn("same QBO account", remark)
        self.assertIn("77", remark)

    def test_identical_names_keep_their_own_message(self):
        out, _ = process_transfers(
            _raw_transfer_df("KZO CBD Z", "KZO CBD Z"),
            country="TH",
            start_no=0,
            qbo_mappings={"accounts": {"KZO CBD Z": "77"}, "locations": {}},
            client_name="KZO",
        )

        self.assertIn("cannot be the same", out["Remarks"].iloc[0])

    def test_distinct_accounts_stay_ready_to_sync(self):
        out, _ = process_transfers(
            _raw_transfer_df("KZO CBD Z", "KZO Bank TH"),
            country="TH",
            start_no=0,
            qbo_mappings={"accounts": {"KZO CBD Z": "77", "KZO Bank TH": "88"}, "locations": {}},
            client_name="KZO",
        )

        self.assertEqual(out["Remarks"].iloc[0], "Ready to sync")


def _response(status: int, payload=None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


FAULT_400 = {
    "Fault": {
        "Error": [{
            "Message": "Business Validation Error",
            "Detail": "You need to choose a different account for the transfer.",
            "code": "6000",
        }],
        "type": "ValidationFault",
    }
}


class QBOErrorReportingTests(unittest.TestCase):
    def _client(self) -> QBOClient:
        client = QBOClient()
        client.realm_id = "9341455236413167"
        client._workspace_authorized = True
        client.access_token = "token"
        client.token_expiry = float("inf")
        return client

    def test_400_fails_immediately_with_the_qbo_reason(self):
        # Retrying a rejected payload cannot change the verdict, and raise_for_status'
        # message ("400 Client Error: Bad Request for url: ...") hides why QBO refused it.
        client = self._client()
        with patch("src.connectors.qbo_client.requests.post", return_value=_response(400, FAULT_400)) as post, \
             patch("src.connectors.qbo_client.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                client.post("/v3/company/9341455236413167/transfer", {"Amount": 1})

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("choose a different account", str(ctx.exception))
        self.assertIn("code 6000", str(ctx.exception))

    def test_400_without_a_fault_envelope_still_reports_the_body(self):
        client = self._client()
        with patch("src.connectors.qbo_client.requests.post",
                   return_value=_response(400, None, text="<html>rejected</html>")), \
             patch("src.connectors.qbo_client.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client.post("/v3/company/9341455236413167/transfer", {})

        self.assertIn("rejected", str(ctx.exception))

    def test_rate_limit_is_still_retried(self):
        client = self._client()
        responses = [_response(429, None, text="throttled"), _response(200, {"Transfer": {"Id": "9"}})]
        with patch("src.connectors.qbo_client.requests.post", side_effect=responses) as post, \
             patch("src.connectors.qbo_client.time.sleep"), \
             patch("builtins.print"):  # retry banner uses emoji; test consoles are cp1252
            out = client.post("/v3/company/9341455236413167/transfer", {})

        self.assertEqual(post.call_count, 2)
        self.assertEqual(out, {"Transfer": {"Id": "9"}})

    def test_401_refreshes_the_token_once_then_gives_up(self):
        client = self._client()
        with patch("src.connectors.qbo_client.requests.post",
                   return_value=_response(401, None, text="unauthorized")) as post, \
             patch("src.connectors.qbo_client.time.sleep"), \
             patch("builtins.print"), \
             patch.object(QBOClient, "refresh_access_token", return_value="fresh") as refresh:
            with self.assertRaises(RuntimeError):
                client.post("/v3/company/9341455236413167/transfer", {})

        # One retry after clearing the cached token; the second 401 is a real auth failure.
        self.assertEqual(post.call_count, 2)
        self.assertTrue(refresh.called)


if __name__ == "__main__":
    unittest.main()
