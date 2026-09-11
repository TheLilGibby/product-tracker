"""
Routes for product groups: the group page, putting a listing in a group, and
renaming or deleting a group. The grouped dashboard itself is rendered by
main.index; this blueprint serves its 30-second refresh fragment.

Nothing here touches a listing's scrape schedule or auto-cart settings.
Grouping only changes how listings are shown together.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import db
from app.groups import (assign_group, clean_group_name, find_group_by_name, find_or_create_group,
                        group_history, group_listings, grouped_view, last_in_stock_times, summarize)
from app.models.product import Product, ProductGroup

groups_bp = Blueprint('groups', __name__)


@groups_bp.app_context_processor
def inject_group_helpers():
    """product_group_names() for the group picker on a listing's page. Only queries when called."""
    def product_group_names():
        return [g.name for g in ProductGroup.query.order_by(ProductGroup.name).all()]
    return {'product_group_names': product_group_names}


@groups_bp.route('/group/<int:group_id>')
def detail(group_id):
    """One product across every retailer: its listings and their stock history."""
    group = db.get_or_404(ProductGroup, group_id)
    listings = group_listings(group)
    return render_template('groups/detail.html', group=group, listings=listings,
                           summary=summarize([row['product'] for row in listings]),
                           last_in_stock=last_in_stock_times([row['product'].id for row in listings]),
                           history=group_history(group))


@groups_bp.route('/groups/partial')
def grouped_partial():
    """The grouped dashboard on its own, for its in-page refresh."""
    groups, ungrouped = grouped_view()
    return render_template('products/_grouped.html', groups=groups, ungrouped=ungrouped)


@groups_bp.route('/product/<int:product_id>/group', methods=['POST'])
def set_product_group(product_id):
    """
    Put a listing in the group named in the form, creating the group if no
    group has that name yet. A blank name takes the listing out of its group.
    """
    product = db.get_or_404(Product, product_id)
    name = clean_group_name(request.form.get('group_name'))
    previous = product.group_membership.group if product.group_membership else None

    if not name:
        if previous is None:
            flash('This listing is not in a group.', 'info')
        else:
            assign_group(product, None)
            db.session.commit()
            flash(f'Removed from "{previous.name}".', 'success')
        return redirect(url_for('main.product_detail', product_id=product.id))

    group = find_or_create_group(name)
    if group is previous:
        flash(f'Already in "{group.name}".', 'info')
        return redirect(url_for('main.product_detail', product_id=product.id))

    assign_group(product, group)
    db.session.commit()
    flash(f'Added to "{group.name}".', 'success')
    return redirect(url_for('main.product_detail', product_id=product.id))


@groups_bp.route('/group/<int:group_id>/rename', methods=['POST'])
def rename(group_id):
    """Rename a group. Refused when another group already has the name."""
    group = db.get_or_404(ProductGroup, group_id)
    name = clean_group_name(request.form.get('name'))
    if not name:
        flash('A group needs a name.', 'danger')
        return redirect(url_for('groups.detail', group_id=group.id))

    clash = find_group_by_name(name)
    if clash is not None and clash.id != group.id:
        flash(f'There is already a group called "{clash.name}".', 'danger')
        return redirect(url_for('groups.detail', group_id=group.id))

    group.name = name
    db.session.commit()
    flash('Group renamed.', 'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/group/<int:group_id>/delete', methods=['POST'])
def delete(group_id):
    """Delete a group. Its listings stay tracked, with their settings, just ungrouped."""
    group = db.get_or_404(ProductGroup, group_id)
    name, count = group.name, len(group.members)
    db.session.delete(group)
    db.session.commit()
    flash(f'Deleted "{name}". Its {count} listing{"" if count == 1 else "s"} are still tracked.', 'success')
    return redirect(url_for('main.index', view='grouped'))
