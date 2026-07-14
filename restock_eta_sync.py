"""
Restock ETA sync -- runs on a schedule via GitHub Actions (see
.github/workflows/sync.yml), no local machine required. For SKUs with an
outstanding pending shipment in Supabase (impos + impo_lines) AND currently at
0 Shopify inventory, sets a custom.restock_eta variant metafield (date) and
flips that variant's inventory policy to CONTINUE (continue selling when out
of stock) so Add to Cart re-enables itself.

Self-cleaning: tracks which SKUs it previously synced in state.json, which is
checked into this repo so the state survives between ephemeral Action runs
(the workflow commits it back after every run). Any SKU that drops out of the
outstanding list on a later run (shipment received/cancelled in Supabase) has
its metafield cleared and inventory policy reverted to DENY, so the storefront
notice disappears automatically instead of lingering with a stale date.
"""
import json
import os
import urllib.request
import urllib.parse
from collections import defaultdict

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
SHOP = "musicmajlistest.myshopify.com"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def supabase_get(base_url, key, path):
    req = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_all_paginated(base_url, key, table, select, page_size=1000):
    rows = []
    offset = 0
    while True:
        path = f"{table}?select={select}&limit={page_size}&offset={offset}"
        batch = supabase_get(base_url, key, path)
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def get_shopify_token(client_id, client_secret):
    url = f"https://{SHOP}/admin/oauth/access_token"
    data = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]


def shopify_graphql(token, query, variables=None):
    url = f"https://{SHOP}/admin/api/2026-04/graphql.json"
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_outstanding_eta(supabase_url, supabase_key):
    impos = fetch_all_paginated(supabase_url, supabase_key, "impos", "id,eta,status")
    impo_lines = fetch_all_paginated(
        supabase_url, supabase_key, "impo_lines", "impo_id,item_code,qty_incoming,qty_received"
    )
    impo_by_id = {i["id"]: i for i in impos}

    earliest_eta = {}
    for line in impo_lines:
        impo = impo_by_id.get(line["impo_id"])
        if not impo or impo["status"] != "pending":
            continue
        received = line.get("qty_received") or 0
        if received >= (line.get("qty_incoming") or 0):
            continue
        code = line["item_code"]
        eta = impo["eta"]
        if code not in earliest_eta or eta < earliest_eta[code]:
            earliest_eta[code] = eta
    return earliest_eta


def find_variant_by_sku(token, sku):
    query = """
    query($query: String!) {
      productVariants(first: 5, query: $query) {
        nodes { id sku inventoryQuantity product { id } }
      }
    }
    """
    result = shopify_graphql(token, query, {"query": f"sku:{sku}"})
    nodes = result.get("data", {}).get("productVariants", {}).get("nodes", [])
    for node in nodes:
        if node["sku"] == sku:
            return node
    return None


def bulk_update(token, product_id, variants):
    mutation = """
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id sku inventoryPolicy }
        userErrors { field message }
      }
    }
    """
    result = shopify_graphql(token, mutation, {"productId": product_id, "variants": variants})
    return result.get("data", {}).get("productVariantsBulkUpdate", {})


def delete_metafields(token, variant_ids):
    mutation = """
    mutation($metafields: [MetafieldIdentifierInput!]!) {
      metafieldsDelete(metafields: $metafields) {
        deletedMetafields { key ownerId }
        userErrors { field message }
      }
    }
    """
    identifiers = [{"ownerId": vid, "namespace": "custom", "key": "restock_eta"} for vid in variant_ids]
    result = shopify_graphql(token, mutation, {"metafields": identifiers})
    return result.get("data", {}).get("metafieldsDelete", {})


def main():
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_KEY"]
    client_id = os.environ["SHOPIFY_CLIENT_ID"]
    client_secret = os.environ["SHOPIFY_CLIENT_SECRET"]
    state = load_state()  # sku -> {variantId, productId, eta}

    print("Fetching outstanding shipment ETAs from Supabase...")
    earliest_eta = fetch_outstanding_eta(supabase_url, supabase_key)
    print(f"Outstanding SKUs: {len(earliest_eta)}")

    token = get_shopify_token(client_id, client_secret)

    definition_mutation = """
    mutation($definition: MetafieldDefinitionInput!) {
      metafieldDefinitionCreate(definition: $definition) {
        createdDefinition { id }
        userErrors { field message code }
      }
    }
    """
    shopify_graphql(token, definition_mutation, {
        "definition": {
            "name": "Restock ETA",
            "namespace": "custom",
            "key": "restock_eta",
            "type": "date",
            "ownerType": "PRODUCTVARIANT",
            "description": "Expected restock date, synced from incoming-shipment tracking. Estimate only.",
        }
    })

    to_update = defaultdict(list)
    updated_count = 0
    new_state = {}

    print("Matching outstanding SKUs to Shopify variants (0-stock only)...")
    for sku, eta in earliest_eta.items():
        variant = find_variant_by_sku(token, sku)
        if not variant or variant["inventoryQuantity"] > 0:
            continue
        new_state[sku] = {"variantId": variant["id"], "productId": variant["product"]["id"], "eta": eta}
        if state.get(sku, {}).get("eta") == eta:
            continue  # already synced with this exact eta, skip the write
        to_update[variant["product"]["id"]].append({
            "id": variant["id"],
            "inventoryPolicy": "CONTINUE",
            "metafields": [{"namespace": "custom", "key": "restock_eta", "value": eta}],
        })
        updated_count += 1

    # Anything previously synced that's no longer outstanding (shipment received/
    # cancelled in Supabase) gets its policy reverted to DENY and its metafield
    # deleted, so the storefront notice disappears instead of showing a stale date
    # forever.
    stale_variant_ids = []
    for sku, entry in state.items():
        if sku in new_state:
            continue
        to_update[entry["productId"]].append({"id": entry["variantId"], "inventoryPolicy": "DENY"})
        stale_variant_ids.append(entry["variantId"])

    print(f"{updated_count} variants to update, {len(stale_variant_ids)} stale entries to clear, across {len(to_update)} products.")

    success = 0
    failed = []
    for product_id, variants in to_update.items():
        payload = bulk_update(token, product_id, variants)
        user_errors = payload.get("userErrors", [])
        if user_errors:
            failed.append((product_id, user_errors))
        else:
            success += len(payload.get("productVariants", []))

    if stale_variant_ids:
        delete_result = delete_metafields(token, stale_variant_ids)
        delete_errors = delete_result.get("userErrors", [])
        if delete_errors:
            failed.append(("metafieldsDelete", delete_errors))

    save_state(new_state)

    print(f"\nDone. {success} variant updates applied ({updated_count} new/changed ETAs, {len(stale_variant_ids)} cleared).")
    if failed:
        print(f"{len(failed)} operations had errors:")
        for pid, errs in failed:
            print(" ", pid, errs)


if __name__ == "__main__":
    main()
