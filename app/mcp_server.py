"""Valio Aimo's MCP interface for purchasing agents. Every agent, ours included, uses these tools with a bearer
token that names its restaurant. There is no tool to approve an order."""
import asyncio
import json
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic import BaseModel, Field

import catalog
import events
import gateway
import store

mcp = FastMCP("Valio Aimo", instructions=(
    "Valio Aimo foodservice wholesale. Search offers with live price and availability, find substitutes, read the "
    "restaurant's purchase history and submit orders. Every order is checked by Valio Aimo's verifier against the "
    "chef's signed instructions; orders outside the chef's mandate wait for the chef's approval on their phone."))


def who(token):
    """The restaurant and agent a bearer token belongs to, or None."""
    row = store.one("select restaurant_id restaurant, agent from tokens where token=?", token)
    return row


def caller():
    auth = get_http_headers(include={"authorization"}).get("authorization", "")
    me = who(auth.removeprefix("Bearer ").strip())
    if not me:
        raise PermissionError("unknown token")
    store.current.set(me["restaurant"])
    return me


async def _run(tool, fn, *args, **kw):
    who = caller()
    events.emit("mcp.call", tool=tool, agent=who["agent"], args=kw or list(args))
    result = await asyncio.to_thread(fn, who["restaurant"], *args, **kw)
    events.emit("mcp.result", tool=tool, agent=who["agent"],
                count=len(result) if isinstance(result, list) else None)
    return result


Unit = Literal["L", "kg", "sales_units"]


@mcp.tool
async def find_products(
    query: Annotated[str, "Short product description in English or Finnish, e.g. 'lactose free milk drink 1 l'"],
    delivery_date: Annotated[str, "YYYY-MM-DD"],
    quantity: Annotated[float | None, "Amount needed, in `unit`"] = None,
    unit: Unit | None = None,
    free_from: Annotated[list[str], "Allergens the product must be stated free from, e.g. ['lactose', 'gluten']. "
                         "Products whose data doesn't state it are left out."] = [],
) -> list[dict]:
    """Offers for a need, ranked: orderable first, then products the restaurant usually buys, then price per litre
    or kilogram. Each offer has live price and availability, the sales units needed for `quantity`, the line total
    and a `problem` when it can't be delivered on the date."""
    return await _run("find_products", gateway.find_products, query, delivery_date, quantity, unit, free_from)


@mcp.tool
async def find_substitutes(product_id: str, delivery_date: str, quantity: float | None = None,
                           unit: Unit | None = None) -> list[dict]:
    """Ranked substitutes for a product within Valio Aimo's assortment, each with a field-by-field comparison:
    Valio-listed substitute, bought before, price change per litre or kg, claims and allergens that differ."""
    return await _run("find_substitutes", gateway.substitutes, product_id, delivery_date, quantity, unit)


@mcp.tool
async def check_offers(product_ids: list[str], delivery_date: str) -> list[dict]:
    """Live price and availability for known product ids."""
    return await _run("check_offers", gateway.check_offers, product_ids, delivery_date)


@mcp.tool
async def order_history() -> list[dict]:
    """The products this restaurant usually buys, with average weekly quantities in sales units."""
    return await _run("order_history", gateway.order_history)


@mcp.tool
async def product_details(product_id: str) -> dict:
    """Full catalog data for one product: names, description, ingredients, allergens, claims, labels."""
    p = await _run("product_details", lambda rid: catalog.get(product_id))
    return {**catalog.summary(p), "ingredients": p["ingredients"], "labels": p["labels"],
            "allergens": {catalog.ALLERGENS.get(k, k): v for k, v in p["allergens"].items()}} if p else {}


class Cite(BaseModel):
    instruction_id: str = Field(description="Id of the chef's signed instruction, e.g. I-3F2A1B")
    quote: str = Field(description="The chef's exact words that ask for this line")


class Line(BaseModel):
    product_id: str
    quantity: float = Field(description="Number of sales units")
    cites: list[Cite] = Field(default=[], description="Where the chef asked for this line")


@mcp.tool
async def submit_order(delivery_date: str, lines: list[Line], note: str | None = None) -> dict:
    """Submits an order. Valio Aimo's verifier checks it against the chef's signed instructions and standing rules
    and decides: placed, held for the chef's approval, returned for you to fix, or rejected. Cite for every line
    the chef's words that ask for it."""
    return await _run("submit_order", lambda rid: gateway.submit_order(
        rid, caller_agent(), delivery_date, [l.model_dump() for l in lines], note))


def caller_agent():
    return caller()["agent"]


@mcp.tool
async def cancel_order(order_id: str, instruction_id: str) -> dict:
    """Cancels an order the chef asked to cancel or change, citing the chef's new signed instruction. Works while
    the order waits for approval or within a minute of it being placed. To change an order, cancel it and submit the
    corrected complete order."""
    return await _run("cancel_order", lambda rid: gateway.cancel(rid, order_id, instruction_id))


@mcp.tool
async def order_status(order_id: str) -> dict:
    """Current status of an order you submitted."""
    o = await _run("order_status", lambda rid: gateway.get_order(order_id))
    return gateway.order_summary(o) if o and o["restaurant_id"] == caller()["restaurant"] else {"error": "not found"}
