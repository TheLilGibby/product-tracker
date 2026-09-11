"""
Product groups: one real product, every retailer listing that sells it.

A Product row is still a single retailer listing. It owns its own price, stock,
auto-cart settings and scrape schedule, and the scheduler and the auto-cart job
work on listings exactly as they did before groups existed. A ProductGroup only
records which listings are the same item, so the dashboard can say "in stock at
2 of 5 retailers" instead of showing five unrelated rows.
"""
from sqlalchemy import func

from app import db
from app.models.product import AvailabilityHistory, Product, ProductGroup, ProductGroupMember
from app.scrapers import STORE_LABELS, detect_store_type

GROUP_NAME_MAX = 200


def store_label(url):
    """The retailer's display name for a listing URL."""
    store_type = detect_store_type(url)
    if not store_type:
        return 'Unknown store'
    return STORE_LABELS.get(store_type, store_type.title())


def clean_group_name(name):
    """Collapse whitespace and cap the length; '' when nothing is left."""
    return ' '.join((name or '').split())[:GROUP_NAME_MAX]


def find_group_by_name(name):
    """The group called `name`, matched case-insensitively, or None."""
    name = clean_group_name(name)
    if not name:
        return None
    return ProductGroup.query.filter(func.lower(ProductGroup.name) == name.lower()).first()


def find_or_create_group(name):
    """
    The group called `name`, created if there is none yet.

    Matching ignores case, so "Zelda console" and "zelda Console" are one group.
    Returns None for a blank name. A new group is added to the session but not
    committed.
    """
    name = clean_group_name(name)
    if not name:
        return None
    group = find_group_by_name(name)
    if group is None:
        group = ProductGroup(name=name)
        db.session.add(group)
    return group


def assign_group(product, group):
    """Move a listing into `group`, or out of any group when group is None. Not committed."""
    if group is None:
        # delete-orphan on Product.group_membership removes the row
        product.group_membership = None
    elif product.group_membership is None:
        product.group_membership = ProductGroupMember(group=group)
    else:
        product.group_membership.group = group


def last_in_stock_times(product_ids):
    """{product_id: newest time that listing was logged in stock}, for listings that ever were."""
    if not product_ids:
        return {}
    rows = (db.session.query(AvailabilityHistory.product_id, func.max(AvailabilityHistory.timestamp))
            .filter(AvailabilityHistory.product_id.in_(product_ids),
                    AvailabilityHistory.available == True)  # noqa: E712 - SQL comparison
            .group_by(AvailabilityHistory.product_id)
            .all())
    return dict(rows)


def summarize(listings):
    """
    Roll a group's listings up into what its grouped row shows.

    The headline price is the cheapest listing that is in stock. Only when
    nothing is in stock does it fall back to the cheapest listing overall,
    because a lower price you cannot buy is not the price that matters on a
    drop day.
    """
    in_stock = [p for p in listings if p.available]
    pool = [p for p in (in_stock or listings) if p.current_price is not None]
    best = min(pool, key=lambda p: p.current_price) if pool else None
    checked = [p.last_checked for p in listings if p.last_checked]
    return {
        'total': len(listings),
        'in_stock': len(in_stock),
        'best': best,
        'best_store': store_label(best.url) if best else None,
        'best_is_in_stock': bool(best and best.available),
        'auto_cart_armed': sum(1 for p in listings if p.auto_cart_enabled),
        'last_checked': max(checked) if checked else None,
    }


def group_listings(group):
    """A group's listings as {'product', 'store'}, in-stock first, then by store name."""
    listings = [{'product': m.product, 'store': store_label(m.product.url)} for m in group.members]
    listings.sort(key=lambda row: (not row['product'].available, row['store'].lower(), row['product'].id))
    return listings


def grouped_view():
    """
    (group rows, ungrouped listings) for the dashboard's grouped view.

    Each group row is {'group', 'listings', 'summary'}. Groups with something
    in stock come first, then A to Z. Ungrouped listings come back as
    {'product', 'store'}, in-stock first.
    """
    rows = []
    for group in ProductGroup.query.order_by(ProductGroup.name).all():
        listings = group_listings(group)
        rows.append({'group': group, 'listings': listings,
                     'summary': summarize([row['product'] for row in listings])})
    rows.sort(key=lambda row: (row['summary']['in_stock'] == 0, row['group'].name.lower()))

    ungrouped = (Product.query.filter(~Product.group_membership.has())
                 .order_by(Product.available.desc(), Product.name).all())
    return rows, [{'product': p, 'store': store_label(p.url)} for p in ungrouped]


def group_history(group, limit=200):
    """Newest-first stock changes across every listing in the group, as {'entry', 'store'}."""
    product_ids = [m.product_id for m in group.members]
    if not product_ids:
        return []
    entries = (AvailabilityHistory.query
               .filter(AvailabilityHistory.product_id.in_(product_ids))
               .order_by(AvailabilityHistory.timestamp.desc(), AvailabilityHistory.id.desc())
               .limit(limit)
               .all())
    return [{'entry': e, 'store': store_label(e.product.url)} for e in entries]
