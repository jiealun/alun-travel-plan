"""Synthetic regressions for place binding and admission scope (no network)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_validate import load, run, item, has_issue
import render_outputs as ro


def inside_meal(status='confirmed'):
    trip = load()
    day = trip['itinerary']['days'][0]
    visit = item(trip, 'item-d1-museum')
    visit['booking_status'] = status
    visit['planned_end'] = '2026-10-10T13:00:00+08:00'
    meal = copy.deepcopy(visit)
    meal.update(item_id='inside-meal', kind='meal', title='馆内用餐',
                planned_start='2026-10-10T13:00:00+08:00',
                planned_end='2026-10-10T14:00:00+08:00',
                booking_status='not_required', incoming_leg_id=None,
                admission_item_id=visit['item_id'], locked=False,
                description='在同一场馆内用餐，营业情况待核；未离场。')
    day['items'].insert(day['items'].index(visit) + 1, meal)
    return trip, meal


class AdmissionTests(unittest.TestCase):
    def test_all_onsite_kinds_cannot_bypass_required_admission(self):
        for kind in ('meal', 'rest', 'free', 'buffer', 'checkin'):
            with self.subTest(kind=kind):
                trip, meal = inside_meal()
                meal.pop('admission_item_id')
                meal['kind'] = kind
                audit, errors = run(trip)
                self.assertEqual(errors, [])
                self.assertTrue(has_issue(audit, 'blocking', '需要预约，但'))

    def test_checkout_does_not_require_new_lodging_reservation(self):
        trip = load()
        checkin = item(trip, 'item-d1-checkin')
        checkout = item(trip, 'item-d2-checkout')
        hotel = next(p for p in trip['places'] if p['place_id'] == checkin['place_id'])
        hotel['booking']['required'] = True
        checkin['booking_status'] = 'confirmed'
        checkout['booking_status'] = 'not_required'
        audit, _ = run(trip)
        self.assertEqual(audit.summary()['blocking'], 0, audit.issues)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trip.json'
            path.write_text(json.dumps(trip, ensure_ascii=False), encoding='utf-8')
            self.assertEqual(ro.main([str(path)]), 0)
            for suffix in ('md', 'html'):
                text = next((path.parent / 'outputs').glob('*.' + suffix)).read_text(encoding='utf-8')
                self.assertIn('场所预约规则：需要（退房不新增预约）', text)

    def test_same_venue_admission_preserves_pending_condition(self):
        for status in ('confirmed', 'pending_user', 'not_open', 'unknown'):
            with self.subTest(status=status):
                trip, _ = inside_meal(status)
                audit, errors = run(trip)
                self.assertEqual(errors, [])
                self.assertEqual(audit.summary()['blocking'], 0, audit.issues)
                if status != 'confirmed':
                    self.assertTrue(any(x['severity'] == 'conditional' and 'inside-meal' in x['affected_ids'] for x in audit.issues))

    def test_invalid_admission_relationships(self):
        for reference in ('missing-id', 'inside-meal', 'item-d2-garden', 'item-d1-meal-1'):
            with self.subTest(reference=reference):
                trip, meal = inside_meal()
                meal['admission_item_id'] = reference
                audit, _ = run(trip)
                self.assertTrue(has_issue(audit, 'blocking', '入场沿用关系无效'))

    def test_leaving_and_reentering_does_not_reuse_admission(self):
        trip, meal = inside_meal()
        exit_item = copy.deepcopy(meal)
        exit_item.update(item_id='exit', kind='transit', place_id=None,
                         admission_item_id=None, planned_end=meal['planned_start'])
        seq = trip['itinerary']['days'][0]['items']
        seq.insert(seq.index(meal), exit_item)
        audit, _ = run(trip)
        self.assertTrue(has_issue(audit, 'blocking', '入场沿用关系无效'))

    def test_inherited_entry_does_not_override_failed_or_unverified_state(self):
        trip, meal = inside_meal('failed')
        audit, _ = run(trip)
        self.assertTrue(has_issue(audit, 'blocking', '预约失败仍在主行程'))
        trip, meal = inside_meal('pending_user')
        meal['verification_status'] = 'verified'
        audit, _ = run(trip)
        self.assertTrue(has_issue(audit, 'blocking', '预约状态 pending_user 却标 verified'))

    def test_onsite_meal_respects_closing_time(self):
        trip, meal = inside_meal()
        meal['planned_end'] = '2026-10-10T18:00:00+08:00'
        audit, _ = run(trip)
        self.assertTrue(has_issue(audit, 'blocking', '不在 示例博物馆 的开放窗口内'))

    def test_already_inside_does_not_require_entry_before_last_entry_again(self):
        trip, meal = inside_meal()
        place = next(p for p in trip['places'] if p['place_id'] == meal['place_id'])
        for window in place['opening_windows']:
            window['last_entry'] = '12:00'
        audit, _ = run(trip)
        self.assertFalse(any(x['severity'] == 'blocking' and 'inside-meal' in x['affected_ids'] for x in audit.issues), audit.issues)

    def test_outside_meal_gets_own_place_and_no_museum_rules(self):
        trip, meal = inside_meal()
        meal.pop('admission_item_id')
        meal.update(place_id='outside-area', title='院外附近午餐', booking_status='unknown')
        place = copy.deepcopy(next(p for p in trip['places'] if p['place_id'] == 'place-oldstreet'))
        place.update(place_id='outside-area', name='院外就餐片区（商户待选）', address=None,
                     opening_windows=[], booking=dict(required=None, channel=None, note=None))
        trip['places'].append(place)
        audit, _ = run(trip)
        self.assertEqual(audit.summary()['blocking'], 0, audit.issues)

    def test_outside_meal_wrong_binding_is_rejected_before_render(self):
        trip, meal = inside_meal()
        meal.pop('admission_item_id')
        meal['title'] = '院外附近午餐（商户现场选）'
        audit, _ = run(trip)
        self.assertTrue(any(x['severity'] == 'blocking' and 'inside-meal' in x['affected_ids'] for x in audit.issues))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trip.json'
            path.write_text(json.dumps(trip, ensure_ascii=False), encoding='utf-8')
            self.assertNotEqual(ro.main([str(path)]), 0)
            self.assertFalse((path.parent / 'outputs').exists())

    def test_rendered_scope_is_visible_in_both_formats(self):
        trip, meal = inside_meal('pending_user')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trip.json'
            path.write_text(json.dumps(trip, ensure_ascii=False), encoding='utf-8')
            self.assertEqual(ro.main([str(path)]), 0)
            for suffix in ('md', 'html'):
                text = next((path.parent / 'outputs').glob('*.' + suffix)).read_text(encoding='utf-8')
                # HTML embedded data does not contain these renderer-generated scope labels.
                self.assertIn('活动预约：无需预约', text)
                self.assertIn('场所入场预约：需要', text)
                self.assertIn('入场沿用「' + item(trip, 'item-d1-museum')['title'] + '」：待你预约', text)


if __name__ == '__main__':
    unittest.main()
