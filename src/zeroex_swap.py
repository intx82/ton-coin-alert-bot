#!/usr/bin/env python3
"""
Minimal 0x Swap API v2 client for Ethereum mainnet:

    BUY:  USDT -> LINK
    SELL: LINK -> USDT

HTTP calls use httpx. Ethereum transactions are signed locally with
eth-account and submitted through an Ethereum JSON-RPC endpoint.

Safety defaults:
  * Read-only unless `swap ... --yes` is supplied.
  * Token approval is exact per trade, not unlimited.
  * Approval is sent only with `--approve`.
  * The private key is read only from EVM_PRIVATE_KEY, never from a CLI option.
  * Each transaction is rejected if its estimated gas cost exceeds
    --max-gas-eth.
  * The allowance target is taken from the live 0x response.
  * The script never approves the 0x Settler contract.

Required environment:
    ZEROX_API_KEY=...
    ETH_RPC_URL=https://...
    EVM_PRIVATE_KEY=0x...       # required only for balances/live transactions

Install:
    python3 -m pip install httpx eth-account

Examples:
    # Derive and print the wallet address
    python3 zeroex_swap.py address

    # Read wallet balances
    python3 zeroex_swap.py balances

    # Indicative prices only
    python3 zeroex_swap.py price buy 100
    python3 zeroex_swap.py price sell 5

    # Preview a firm quote; nothing is sent
    python3 zeroex_swap.py swap buy 100

    # Approve exact USDT amount if needed, then execute
    python3 zeroex_swap.py swap buy 100 --approve --yes

    # Sell LINK back to USDT
    python3 zeroex_swap.py swap sell 5 --approve --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from eth_account import Account
from eth_utils import is_address, to_checksum_address


ZEROX_API_URL = "https://api.0x.org"
CHAIN_ID = 1

USDT_ADDRESS = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
LINK_ADDRESS = "0x514910771AF9Ca656af840dff83E8264EcF986CA"

BALANCE_OF_SELECTOR = "70a08231"
ALLOWANCE_SELECTOR = "dd62ed3e"
APPROVE_SELECTOR = "095ea7b3"

TERMINAL_RECEIPT_TIMEOUT = 300.0
RECEIPT_POLL_INTERVAL = 3.0


class SwapError(RuntimeError):
    """Expected application, API, or RPC error."""


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class Pair:
    sell: Token
    buy: Token


USDT = Token("USDT", to_checksum_address(USDT_ADDRESS), 6)
LINK = Token("LINK", to_checksum_address(LINK_ADDRESS), 18)


def parse_positive_decimal(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal amount: {value}") from exc

    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("amount must be positive and finite")
    return amount


def parse_nonnegative_decimal(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal amount: {value}") from exc

    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("amount must be non-negative and finite")
    return amount


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def to_base_units(amount: Decimal, token: Token) -> int:
    scaled = amount * (Decimal(10) ** token.decimals)
    integral = scaled.to_integral_value()

    if scaled != integral:
        raise SwapError(
            f"{token.symbol} supports at most {token.decimals} decimal places"
        )

    result = int(integral)
    if result <= 0:
        raise SwapError("amount is too small")
    return result


def from_base_units(amount: int | str, token: Token) -> Decimal:
    return Decimal(int(amount)) / (Decimal(10) ** token.decimals)


def pair_for_direction(direction: str) -> Pair:
    if direction == "buy":
        return Pair(sell=USDT, buy=LINK)
    if direction == "sell":
        return Pair(sell=LINK, buy=USDT)
    raise SwapError(f"unsupported direction: {direction}")


def normalize_hex_private_key(value: str | None) -> str | None:
    if value is None:
        return None

    key = value.strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    return key


def require_private_key() -> str:
    private_key = normalize_hex_private_key(os.environ.get("EVM_PRIVATE_KEY"))
    if not private_key:
        raise SwapError(
            "EVM_PRIVATE_KEY is required for this operation; set it in the "
            "environment, not on the command line"
        )

    try:
        Account.from_key(private_key)
    except Exception as exc:
        raise SwapError("EVM_PRIVATE_KEY is invalid") from exc
    return private_key


def wallet_from_private_key(private_key: str) -> str:
    return to_checksum_address(Account.from_key(private_key).address)


def resolve_wallet(explicit_wallet: str | None) -> str:
    if explicit_wallet:
        if not is_address(explicit_wallet):
            raise SwapError(f"invalid Ethereum wallet address: {explicit_wallet}")
        return to_checksum_address(explicit_wallet)

    private_key = normalize_hex_private_key(os.environ.get("EVM_PRIVATE_KEY"))
    if private_key:
        try:
            return wallet_from_private_key(private_key)
        except Exception as exc:
            raise SwapError("EVM_PRIVATE_KEY is invalid") from exc

    wallet = os.environ.get("EVM_WALLET")
    if wallet:
        if not is_address(wallet):
            raise SwapError("EVM_WALLET is not a valid Ethereum address")
        return to_checksum_address(wallet)

    raise SwapError(
        "wallet address is required: set EVM_PRIVATE_KEY, set EVM_WALLET, "
        "or pass --wallet"
    )


def address_word(address: str) -> str:
    checksum = to_checksum_address(address)
    return checksum[2:].lower().rjust(64, "0")


def uint256_word(value: int) -> str:
    if value < 0 or value >= 2**256:
        raise SwapError("uint256 value is out of range")
    return hex(value)[2:].rjust(64, "0")


def encode_balance_of(owner: str) -> str:
    return "0x" + BALANCE_OF_SELECTOR + address_word(owner)


def encode_allowance(owner: str, spender: str) -> str:
    return (
        "0x"
        + ALLOWANCE_SELECTOR
        + address_word(owner)
        + address_word(spender)
    )


def encode_approve(spender: str, amount: int) -> str:
    return (
        "0x"
        + APPROVE_SELECTOR
        + address_word(spender)
        + uint256_word(amount)
    )


class JsonRpcClient:
    def __init__(self, rpc_url: str, timeout: float) -> None:
        if not rpc_url:
            raise SwapError("ETH_RPC_URL is required")

        self.rpc_url = rpc_url
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "zeroex-swap/1.0",
            },
        )
        self._request_id = 0

    def close(self) -> None:
        self.client.close()

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        try:
            response = self.client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise SwapError(f"Ethereum RPC timed out during {method}") from exc
        except httpx.HTTPError as exc:
            raise SwapError(f"Ethereum RPC HTTP error during {method}: {exc}") from exc
        except ValueError as exc:
            raise SwapError(f"Ethereum RPC returned invalid JSON during {method}") from exc

        if "error" in data:
            error = data["error"]
            raise SwapError(
                f"Ethereum RPC {method} failed: "
                f"{json.dumps(error, ensure_ascii=False)}"
            )

        if "result" not in data:
            raise SwapError(f"Ethereum RPC {method} returned no result")

        return data["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def eth_balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def token_balance(self, token: Token, owner: str) -> int:
        result = self.call(
            "eth_call",
            [
                {
                    "to": token.address,
                    "data": encode_balance_of(owner),
                },
                "latest",
            ],
        )
        return int(result, 16)

    def token_allowance(self, token: Token, owner: str, spender: str) -> int:
        result = self.call(
            "eth_call",
            [
                {
                    "to": token.address,
                    "data": encode_allowance(owner, spender),
                },
                "latest",
            ],
        )
        return int(result, 16)

    def pending_nonce(self, address: str) -> int:
        return int(
            self.call("eth_getTransactionCount", [address, "pending"]),
            16,
        )

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice", []), 16)

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        rpc_tx = {
            key: value
            for key, value in tx.items()
            if key in {"from", "to", "data", "value"}
        }
        if isinstance(rpc_tx.get("value"), int):
            rpc_tx["value"] = hex(rpc_tx["value"])
        return int(self.call("eth_estimateGas", [rpc_tx]), 16)

    def send_raw_transaction(self, raw_transaction: bytes) -> str:
        return self.call(
            "eth_sendRawTransaction",
            ["0x" + raw_transaction.hex()],
        )

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = self.call("eth_getTransactionReceipt", [tx_hash])
        if result is None:
            return None
        if not isinstance(result, dict):
            raise SwapError("Ethereum RPC returned an invalid receipt")
        return result

    def wait_for_receipt(
        self,
        tx_hash: str,
        timeout: float = TERMINAL_RECEIPT_TIMEOUT,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            receipt = self.receipt(tx_hash)
            if receipt is not None:
                status = int(receipt.get("status", "0x0"), 16)
                if status != 1:
                    raise SwapError(f"transaction reverted: {tx_hash}")
                return receipt
            time.sleep(RECEIPT_POLL_INTERVAL)

        raise SwapError(
            f"transaction is still pending after {timeout:.0f}s: {tx_hash}"
        )


class ZeroXClient:
    def __init__(self, api_key: str, timeout: float) -> None:
        if not api_key:
            raise SwapError(
                "ZEROX_API_KEY is required; create an app in the 0x dashboard"
            )

        self.client = httpx.Client(
            base_url=ZEROX_API_URL,
            timeout=timeout,
            headers={
                "0x-api-key": api_key,
                "0x-version": "v2",
                "Accept": "application/json",
                "User-Agent": "zeroex-swap/1.0",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.client.get(endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise SwapError(f"0x API timed out for {endpoint}") from exc
        except httpx.HTTPError as exc:
            raise SwapError(f"0x API network error for {endpoint}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            body = response.text[:500]
            raise SwapError(
                f"0x API returned HTTP {response.status_code} and invalid JSON: "
                f"{body!r}"
            ) from exc

        if response.is_error:
            reason = data.get("reason") or data.get("message") or data
            raise SwapError(
                f"0x API HTTP {response.status_code}: "
                f"{json.dumps(reason, ensure_ascii=False)}"
            )

        if not isinstance(data, dict):
            raise SwapError("0x API returned an invalid response")
        return data

    def price(
        self,
        pair: Pair,
        sell_amount: int,
        taker: str,
        slippage_bps: int,
    ) -> dict[str, Any]:
        return self._get(
            "/swap/allowance-holder/price",
            {
                "chainId": str(CHAIN_ID),
                "sellToken": pair.sell.address,
                "buyToken": pair.buy.address,
                "sellAmount": str(sell_amount),
                "taker": taker,
                "slippageBps": str(slippage_bps),
            },
        )

    def quote(
        self,
        pair: Pair,
        sell_amount: int,
        taker: str,
        slippage_bps: int,
    ) -> dict[str, Any]:
        return self._get(
            "/swap/allowance-holder/quote",
            {
                "chainId": str(CHAIN_ID),
                "sellToken": pair.sell.address,
                "buyToken": pair.buy.address,
                "sellAmount": str(sell_amount),
                "taker": taker,
                "slippageBps": str(slippage_bps),
            },
        )


def validate_chain(rpc: JsonRpcClient) -> None:
    actual = rpc.chain_id()
    if actual != CHAIN_ID:
        raise SwapError(
            f"RPC is connected to chain ID {actual}; Ethereum mainnet "
            f"chain ID {CHAIN_ID} is required"
        )


def issue_object(response: dict[str, Any]) -> dict[str, Any]:
    issues = response.get("issues")
    if issues is None:
        return {}
    if not isinstance(issues, dict):
        raise SwapError("0x response contains an invalid issues object")
    return issues


def validate_0x_response(
    response: dict[str, Any],
    pair: Pair,
    sell_amount: int,
    require_transaction: bool,
) -> None:
    if response.get("liquidityAvailable") is not True:
        raise SwapError("0x reports no available liquidity for this swap")

    if str(response.get("sellToken", "")).lower() != pair.sell.address.lower():
        raise SwapError("0x response sell token does not match the request")

    if str(response.get("buyToken", "")).lower() != pair.buy.address.lower():
        raise SwapError("0x response buy token does not match the request")

    if int(response.get("sellAmount", -1)) != sell_amount:
        raise SwapError("0x response sell amount does not match the request")

    buy_amount = int(response.get("buyAmount", 0))
    min_buy_amount = int(response.get("minBuyAmount", 0))
    if buy_amount <= 0 or min_buy_amount <= 0:
        raise SwapError("0x returned an invalid output amount")
    if min_buy_amount > buy_amount:
        raise SwapError("0x minimum output exceeds quoted output")

    issues = issue_object(response)
    invalid_sources = issues.get("invalidSourcesPassed")
    if invalid_sources:
        raise SwapError(f"0x rejected liquidity sources: {invalid_sources}")

    if require_transaction:
        transaction = response.get("transaction")
        if not isinstance(transaction, dict):
            raise SwapError("firm quote has no transaction object")

        tx_to = transaction.get("to")
        if not isinstance(tx_to, str) or not is_address(tx_to):
            raise SwapError("firm quote transaction has an invalid destination")

        tx_data = transaction.get("data")
        if not isinstance(tx_data, str) or not tx_data.startswith("0x"):
            raise SwapError("firm quote transaction has invalid calldata")


def print_price_or_quote(
    response: dict[str, Any],
    pair: Pair,
    requested_amount: Decimal,
) -> None:
    buy_amount = from_base_units(response["buyAmount"], pair.buy)
    min_buy_amount = from_base_units(response["minBuyAmount"], pair.buy)

    print(f"Pair:               {pair.sell.symbol} -> {pair.buy.symbol}")
    print(
        f"Sell:               {decimal_text(requested_amount)} "
        f"{pair.sell.symbol}"
    )
    print(
        f"Quoted receive:     {decimal_text(buy_amount)} "
        f"{pair.buy.symbol}"
    )
    print(
        f"Minimum receive:    {decimal_text(min_buy_amount)} "
        f"{pair.buy.symbol}"
    )

    if requested_amount > 0:
        rate = buy_amount / requested_amount
        print(
            f"Effective rate:     {decimal_text(rate)} "
            f"{pair.buy.symbol}/{pair.sell.symbol}"
        )

    total_network_fee = int(response.get("totalNetworkFee", 0))
    print(
        f"Estimated network:  "
        f"{decimal_text(from_base_units(total_network_fee, Token('ETH', '', 18)))} "
        f"ETH"
    )

    fees = response.get("fees")
    if isinstance(fees, dict):
        zeroex_fee = fees.get("zeroExFee")
        if isinstance(zeroex_fee, dict) and zeroex_fee.get("amount"):
            fee_token_address = str(zeroex_fee.get("token", "")).lower()
            fee_token = (
                pair.buy
                if fee_token_address == pair.buy.address.lower()
                else pair.sell
                if fee_token_address == pair.sell.address.lower()
                else None
            )
            if fee_token:
                fee_amount = from_base_units(zeroex_fee["amount"], fee_token)
                print(
                    f"0x fee:             {decimal_text(fee_amount)} "
                    f"{fee_token.symbol}"
                )
            else:
                print(f"0x fee raw:         {zeroex_fee}")

    issues = issue_object(response)
    allowance_issue = issues.get("allowance")
    balance_issue = issues.get("balance")
    simulation_incomplete = issues.get("simulationIncomplete")

    if allowance_issue:
        print(
            "Allowance needed:   "
            f"{allowance_issue.get('actual')} base units currently; "
            f"spender={allowance_issue.get('spender')}"
        )
    else:
        print("Allowance needed:   no")

    if balance_issue:
        actual = int(balance_issue.get("actual", 0))
        expected = int(balance_issue.get("expected", 0))
        print(
            f"Balance issue:      have "
            f"{decimal_text(from_base_units(actual, pair.sell))} "
            f"{pair.sell.symbol}, need "
            f"{decimal_text(from_base_units(expected, pair.sell))}"
        )

    if simulation_incomplete:
        print("Simulation:         incomplete")


def gas_cost_eth(gas: int, gas_price: int) -> Decimal:
    return Decimal(gas * gas_price) / Decimal(10**18)


def enforce_gas_limit(
    description: str,
    gas: int,
    gas_price: int,
    max_gas_eth: Decimal,
) -> None:
    cost = gas_cost_eth(gas, gas_price)
    print(
        f"{description} gas:    {gas} @ {gas_price} wei "
        f"(maximum {decimal_text(cost)} ETH)"
    )
    if cost > max_gas_eth:
        raise SwapError(
            f"{description} estimated gas cost {cost} ETH exceeds "
            f"--max-gas-eth {max_gas_eth}"
        )


def sign_and_send(
    rpc: JsonRpcClient,
    private_key: str,
    transaction: dict[str, Any],
) -> str:
    try:
        signed = Account.sign_transaction(transaction, private_key)
    except Exception as exc:
        raise SwapError(f"failed to sign transaction: {exc}") from exc

    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raise SwapError("eth-account returned no raw transaction")

    return rpc.send_raw_transaction(bytes(raw))


def send_approval(
    rpc: JsonRpcClient,
    private_key: str,
    owner: str,
    token: Token,
    spender: str,
    amount: int,
    max_gas_eth: Decimal,
) -> str:
    gas_price = rpc.gas_price()
    nonce = rpc.pending_nonce(owner)
    data = encode_approve(spender, amount)

    estimate_input = {
        "from": owner,
        "to": token.address,
        "data": data,
        "value": 0,
    }
    estimated_gas = rpc.estimate_gas(estimate_input)
    gas = max(estimated_gas * 12 // 10, estimated_gas + 5_000)

    enforce_gas_limit("Approval", gas, gas_price, max_gas_eth)

    tx = {
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "to": token.address,
        "value": 0,
        "data": data,
        "gas": gas,
        "gasPrice": gas_price,
    }

    tx_hash = sign_and_send(rpc, private_key, tx)
    print(f"Approval tx:        {tx_hash}")
    receipt = rpc.wait_for_receipt(tx_hash)
    print(
        f"Approval confirmed: block "
        f"{int(receipt['blockNumber'], 16)}"
    )
    return tx_hash


def ensure_exact_allowance(
    rpc: JsonRpcClient,
    private_key: str,
    owner: str,
    token: Token,
    spender: str,
    required_amount: int,
    max_gas_eth: Decimal,
) -> None:
    current = rpc.token_allowance(token, owner, spender)
    print(
        f"Current allowance:  "
        f"{decimal_text(from_base_units(current, token))} {token.symbol}"
    )

    if current >= required_amount:
        print("Approval:           existing allowance is sufficient")
        return

    # Ethereum USDT rejects a non-zero -> non-zero allowance change.
    if token.address.lower() == USDT.address.lower() and current != 0:
        print("Approval:           resetting USDT allowance to zero")
        send_approval(
            rpc=rpc,
            private_key=private_key,
            owner=owner,
            token=token,
            spender=spender,
            amount=0,
            max_gas_eth=max_gas_eth,
        )

    print(
        f"Approval:           granting exactly "
        f"{decimal_text(from_base_units(required_amount, token))} "
        f"{token.symbol}"
    )
    send_approval(
        rpc=rpc,
        private_key=private_key,
        owner=owner,
        token=token,
        spender=spender,
        amount=required_amount,
        max_gas_eth=max_gas_eth,
    )

    final_allowance = rpc.token_allowance(token, owner, spender)
    if final_allowance < required_amount:
        raise SwapError(
            f"allowance remains insufficient after approval: {final_allowance}"
        )


def build_swap_transaction(
    rpc: JsonRpcClient,
    owner: str,
    quote: dict[str, Any],
    max_gas_eth: Decimal,
) -> dict[str, Any]:
    transaction = quote["transaction"]
    quote_gas = int(transaction["gas"])
    quote_gas_price = int(transaction["gasPrice"])
    current_gas_price = rpc.gas_price()
    gas_price = max(quote_gas_price, current_gas_price)
    gas = max(quote_gas * 12 // 10, quote_gas + 10_000)

    enforce_gas_limit("Swap", gas, gas_price, max_gas_eth)

    value = int(transaction.get("value", "0"))
    eth_balance = rpc.eth_balance(owner)
    required_eth = value + gas * gas_price
    if eth_balance < required_eth:
        raise SwapError(
            f"insufficient ETH for swap gas: have "
            f"{from_base_units(eth_balance, Token('ETH', '', 18))} ETH, "
            f"need at most "
            f"{from_base_units(required_eth, Token('ETH', '', 18))} ETH"
        )

    return {
        "chainId": CHAIN_ID,
        "nonce": rpc.pending_nonce(owner),
        "to": to_checksum_address(transaction["to"]),
        "value": value,
        "data": transaction["data"],
        "gas": gas,
        "gasPrice": gas_price,
    }


def command_address() -> int:
    private_key = require_private_key()
    print(wallet_from_private_key(private_key))
    return 0


def command_balances(rpc: JsonRpcClient, wallet: str) -> int:
    validate_chain(rpc)
    eth_balance = rpc.eth_balance(wallet)
    usdt_balance = rpc.token_balance(USDT, wallet)
    link_balance = rpc.token_balance(LINK, wallet)

    print(f"Wallet: {wallet}")
    print(
        f"ETH:    "
        f"{decimal_text(from_base_units(eth_balance, Token('ETH', '', 18)))}"
    )
    print(f"USDT:   {decimal_text(from_base_units(usdt_balance, USDT))}")
    print(f"LINK:   {decimal_text(from_base_units(link_balance, LINK))}")
    return 0


def command_price(
    zeroex: ZeroXClient,
    pair: Pair,
    amount: Decimal,
    sell_amount: int,
    wallet: str,
    slippage_bps: int,
) -> int:
    response = zeroex.price(
        pair=pair,
        sell_amount=sell_amount,
        taker=wallet,
        slippage_bps=slippage_bps,
    )
    validate_0x_response(
        response=response,
        pair=pair,
        sell_amount=sell_amount,
        require_transaction=False,
    )
    print_price_or_quote(response, pair, amount)
    return 0


def command_swap(
    zeroex: ZeroXClient,
    rpc: JsonRpcClient,
    pair: Pair,
    amount: Decimal,
    sell_amount: int,
    wallet: str,
    slippage_bps: int,
    max_gas_eth: Decimal,
    allow_approval: bool,
    confirmed: bool,
) -> int:
    validate_chain(rpc)

    token_balance = rpc.token_balance(pair.sell, wallet)
    if token_balance < sell_amount:
        raise SwapError(
            f"insufficient {pair.sell.symbol}: have "
            f"{decimal_text(from_base_units(token_balance, pair.sell))}, "
            f"need {decimal_text(amount)}"
        )

    price = zeroex.price(
        pair=pair,
        sell_amount=sell_amount,
        taker=wallet,
        slippage_bps=slippage_bps,
    )
    validate_0x_response(
        response=price,
        pair=pair,
        sell_amount=sell_amount,
        require_transaction=False,
    )

    print("INDICATIVE PRICE")
    print("----------------")
    print_price_or_quote(price, pair, amount)
    print()

    issues = issue_object(price)
    balance_issue = issues.get("balance")
    if balance_issue:
        raise SwapError("0x reports an insufficient token balance")

    allowance_issue = issues.get("allowance")
    spender = price.get("allowanceTarget")
    if allowance_issue:
        spender = allowance_issue.get("spender") or spender

    if not isinstance(spender, str) or not is_address(spender):
        raise SwapError("0x returned no valid AllowanceHolder address")
    spender = to_checksum_address(spender)

    current_allowance = rpc.token_allowance(pair.sell, wallet, spender)
    approval_needed = current_allowance < sell_amount

    if not confirmed:
        quote = zeroex.quote(
            pair=pair,
            sell_amount=sell_amount,
            taker=wallet,
            slippage_bps=slippage_bps,
        )
        validate_0x_response(
            response=quote,
            pair=pair,
            sell_amount=sell_amount,
            require_transaction=True,
        )

        print("FIRM QUOTE PREVIEW")
        print("------------------")
        print_price_or_quote(quote, pair, amount)
        print(f"AllowanceHolder:    {spender}")
        print(f"Transaction target: {quote['transaction']['to']}")
        print()
        print("No transaction was signed or submitted.")
        if approval_needed:
            print(
                "Live execution requires both --approve and --yes because "
                "token approval is currently insufficient."
            )
        else:
            print("Live execution requires --yes.")
        return 0

    private_key = require_private_key()
    derived_wallet = wallet_from_private_key(private_key)
    if derived_wallet.lower() != wallet.lower():
        raise SwapError(
            f"private key belongs to {derived_wallet}, not requested wallet "
            f"{wallet}"
        )

    if approval_needed:
        if not allow_approval:
            raise SwapError(
                "token approval is insufficient; re-run with --approve --yes"
            )

        ensure_exact_allowance(
            rpc=rpc,
            private_key=private_key,
            owner=wallet,
            token=pair.sell,
            spender=spender,
            required_amount=sell_amount,
            max_gas_eth=max_gas_eth,
        )

    # Fetch a fresh firm quote only after allowance is confirmed.
    quote = zeroex.quote(
        pair=pair,
        sell_amount=sell_amount,
        taker=wallet,
        slippage_bps=slippage_bps,
    )
    validate_0x_response(
        response=quote,
        pair=pair,
        sell_amount=sell_amount,
        require_transaction=True,
    )

    quote_issues = issue_object(quote)
    if quote_issues.get("allowance"):
        raise SwapError(
            "0x still reports insufficient allowance after approval; "
            "do not submit"
        )
    if quote_issues.get("balance"):
        raise SwapError("0x reports insufficient balance for the firm quote")
    if quote_issues.get("simulationIncomplete"):
        raise SwapError(
            "0x could not complete quote simulation; refusing live execution"
        )

    quote_target = to_checksum_address(quote["transaction"]["to"])
    allowance_target = to_checksum_address(quote["allowanceTarget"])
    if quote_target.lower() != allowance_target.lower():
        raise SwapError(
            "AllowanceHolder quote transaction target differs from its "
            "allowance target; refusing to submit"
        )

    print("FRESH FIRM QUOTE")
    print("----------------")
    print_price_or_quote(quote, pair, amount)
    print(f"AllowanceHolder:    {allowance_target}")

    tx = build_swap_transaction(
        rpc=rpc,
        owner=wallet,
        quote=quote,
        max_gas_eth=max_gas_eth,
    )

    tx_hash = sign_and_send(rpc, private_key, tx)
    print(f"Swap tx:            {tx_hash}")
    receipt = rpc.wait_for_receipt(tx_hash)
    print(
        f"Swap confirmed:     block "
        f"{int(receipt['blockNumber'], 16)}"
    )

    new_sell_balance = rpc.token_balance(pair.sell, wallet)
    new_buy_balance = rpc.token_balance(pair.buy, wallet)
    print(
        f"{pair.sell.symbol} balance:  "
        f"{decimal_text(from_base_units(new_sell_balance, pair.sell))}"
    )
    print(
        f"{pair.buy.symbol} balance:   "
        f"{decimal_text(from_base_units(new_buy_balance, pair.buy))}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Trade USDT and LINK on Ethereum through 0x Swap API v2 "
            "using httpx."
        )
    )
    parser.add_argument(
        "--wallet",
        help=(
            "Ethereum taker address. Otherwise derived from EVM_PRIVATE_KEY "
            "or read from EVM_WALLET."
        ),
    )
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("ETH_RPC_URL"),
        help="Ethereum mainnet JSON-RPC URL; defaults to ETH_RPC_URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ZEROX_API_KEY"),
        help="0x API key; prefer ZEROX_API_KEY",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=100,
        help="Maximum slippage in basis points (default: 100 = 1%%)",
    )
    parser.add_argument(
        "--max-gas-eth",
        type=parse_nonnegative_decimal,
        default=Decimal("0.02"),
        help=(
            "Maximum estimated ETH cost allowed per transaction "
            "(default: 0.02)"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "address",
        help="Print the Ethereum address derived from EVM_PRIVATE_KEY",
    )

    subparsers.add_parser(
        "balances",
        help="Read ETH, USDT, and LINK balances",
    )

    price = subparsers.add_parser(
        "price",
        help="Request an indicative 0x price; never sends a transaction",
    )
    price.add_argument("direction", choices=("buy", "sell"))
    price.add_argument(
        "amount",
        type=parse_positive_decimal,
        help="USDT amount for buy, LINK amount for sell",
    )

    swap = subparsers.add_parser(
        "swap",
        help="Preview or execute a swap",
    )
    swap.add_argument("direction", choices=("buy", "sell"))
    swap.add_argument(
        "amount",
        type=parse_positive_decimal,
        help="USDT amount for buy, LINK amount for sell",
    )
    swap.add_argument(
        "--approve",
        action="store_true",
        help=(
            "Allow the script to submit an exact ERC-20 approval if needed"
        ),
    )
    swap.add_argument(
        "--yes",
        action="store_true",
        help="Sign and submit the live swap",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not 1 <= args.slippage_bps <= 10_000:
        parser.error("--slippage-bps must be between 1 and 10000")

    zeroex: ZeroXClient | None = None
    rpc: JsonRpcClient | None = None

    try:
        if args.command == "address":
            return command_address()

        wallet = resolve_wallet(args.wallet)

        if not args.rpc_url:
            raise SwapError("ETH_RPC_URL or --rpc-url is required")
        rpc = JsonRpcClient(args.rpc_url, args.timeout)

        if args.command == "balances":
            return command_balances(rpc, wallet)

        if not args.api_key:
            raise SwapError("ZEROX_API_KEY or --api-key is required")
        zeroex = ZeroXClient(args.api_key, args.timeout)

        pair = pair_for_direction(args.direction)
        sell_amount = to_base_units(args.amount, pair.sell)

        if args.command == "price":
            return command_price(
                zeroex=zeroex,
                pair=pair,
                amount=args.amount,
                sell_amount=sell_amount,
                wallet=wallet,
                slippage_bps=args.slippage_bps,
            )

        if args.command == "swap":
            return command_swap(
                zeroex=zeroex,
                rpc=rpc,
                pair=pair,
                amount=args.amount,
                sell_amount=sell_amount,
                wallet=wallet,
                slippage_bps=args.slippage_bps,
                max_gas_eth=args.max_gas_eth,
                allow_approval=args.approve,
                confirmed=args.yes,
            )

        parser.error(f"unsupported command: {args.command}")
        return 2

    except SwapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        if zeroex is not None:
            zeroex.close()
        if rpc is not None:
            rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
