#!/bin/env python

import argparse
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import accumulate, groupby
from math import isfinite
from zoneinfo import ZoneInfo

import portion as intervals
import requests
import toolz
from numpy import ndarray, uint8
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

logging.basicConfig()
logger = logging.getLogger('aula-verdi')


def edisu_fmt_day(time_obj):
    return time_obj.strftime('%d-%m-%Y')


def edisu_fmt_hour(timedelta_obj):
    minutes = timedelta_obj.seconds // 60
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}'


def edisu_parse_hour(string):
    return timedelta(**toolz.valmap(int, re.fullmatch("(?P<hours>.*?):(?P<minutes>.*)", string).groupdict()))


def regex_validator(regex_str):
    regex = re.compile(regex_str)

    def _regex_validator(string):
        match = regex.fullmatch(string)
        if match is None:
            raise argparse.ArgumentTypeError(f'valore errato: {string} (regex: {regex_str})')
        return match.group(0)
    return _regex_validator


rooms = {
    'michelangelo': 1,
    'ormea': 3,
    'verdi': 6
}


def main():
    tz_rome = ZoneInfo("Europe/Rome")
    now = datetime.now(tz=tz_rome)
    parser = argparse.ArgumentParser(description='Prenota un\'aula edisu.')
    parser.add_argument('-l', metavar='email:password', required=True, type=regex_validator('.+@.*:.*'),
                        help='Credenziali')
    parser.add_argument('-a', metavar='aula', default='verdi', choices=rooms.keys(), help='Aula studio da prenotare')
    period_spec = parser.add_mutually_exclusive_group(required=True)
    period_spec.add_argument('-g', metavar='GG-MM-AA', nargs=2, type=regex_validator('\\d{1,2}\\-\\d{1,2}\\-\\d{4}'),
                             help='Giorno di inizio e fine (inclusi) della prenotazione')
    period_spec.add_argument('-p', metavar='#', type=regex_validator('\\d+'),
                             help='Prenota a partire da oggi per i prossimi # giorni')
    parser.add_argument('-o', metavar='hh:mm', nargs=2, required=True, type=regex_validator('\\d{1,2}:(?:00|30)'),
                        help='Ora di inizio e fine della prenotazione (formato 24h)')
    parser.add_argument('-e', metavar='##', default='', type=regex_validator('[1-7]*'),
                        help='Giorni della settimana da escludere (ad esempio "67" per sabato e domenica)')
    parser.add_argument('-n', help='Non effettuare la prenotazione (dry-run)', action='store_true', default=False)
    parser.add_argument('-v', action='count', default=0, help='Aumenta verbosità di logging')
    args = parser.parse_args()
    if args.v == 1:
        logging.getLogger().setLevel(logging.INFO)
    elif min(2, args.v) == 2:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.p is not None:
        day_start = now.replace(microsecond=0, second=0, minute=0, hour=0)
        day_end = day_start
        for i in range(int(args.p)):
            day_end += timedelta(days=1)
    else:
        if args.g[0] < edisu_fmt_day(now):
            args.g[0] = edisu_fmt_day(now)
        day_start, day_end = (datetime.strptime(day, '%d-%m-%Y').replace(tzinfo=tz_rome) for day in args.g)
    if day_start > day_end:
        raise ValueError(f'Data di inizio {edisu_fmt_day(day_start)} maggiore della data di fine '
                         f'{edisu_fmt_day(day_end)}')
    desired_hour_start, desired_hour_end = (edisu_parse_hour(o) for o in args.o)
    if desired_hour_start >= desired_hour_end:
        raise ValueError(f'Ora di inizio {desired_hour_start} maggiore dell\'ora di fine {desired_hour_end}')
    email, password = args.l.split(':', 1)
    room_id = rooms[args.a]
    room_name_id = f'{args.a.upper()} ({room_id})'

    # login through the web API
    login_msg = requests.post('https://edisuprenotazioni.edisu-piemonte.it:8443/sbs/web/signin',
                              data={'email': email, 'password': password}).json()
    if "token" not in login_msg:
        logger.error(f'Login errato: {login_msg["message"]}')
    token = login_msg["token"]
    # this session is used to list bookings, seats and slots
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {token}'})
    """
    Use the the Android app API for the actual booking, because the web API does not allow specifying the seat number...
    This API was reverse engineered by using mitmproxy and an Android emulator. The API appears to use the
    same tokens as the web API. For unknown reasons, it requires the "Accept-Language: it" header.
    """
    book_session = requests.Session()
    book_session.headers.update({'Authorization': f'Bearer {token}', 'Accept-Language': 'it'})

    # run the main algorithm day by day
    class DaySkip(Exception):
        pass

    day = day_start
    while day <= day_end:
        try:
            if str(day.isoweekday()) in args.e:
                raise DaySkip()
            """
            Initialization and sanity checks: fetch the list of slots, seats and bookings for this day, check that the
            requested hours are available, and if booking for today, set hour_start to the current slot.
            """
            valid_slots_msg = session.post('https://edisuprenotazioni.edisu-piemonte.it:8443/sbs/web/student/slots',
                                           data={'date': edisu_fmt_day(day), 'hall': room_name_id}).json()
            logger.debug(f'/sbs/web/student/slots date={edisu_fmt_day(day)}: {valid_slots_msg}')
            if not ((valid_slots_msg.get('result') or {}).get('data') or {}).get('list') or []:
                raise RuntimeError(f'impossibile ottenere la lista di slot per il giorno {edisu_fmt_day(day)}: '
                                   f'{valid_slots_msg["message"]}')
            seats_msg = session.post('https://edisuprenotazioni.edisu-piemonte.it:8443/sbs/web/student/seats',
                                     data={'date': edisu_fmt_day(day), 'hall': room_name_id}).json()
            logger.debug(f'/sbs/web/student/seats date={edisu_fmt_day(day)}: {seats_msg}')
            if not (seats_msg.get('result') or {}).get('seats') or []:
                raise RuntimeError(f'impossibile ottenere la lista posti per il giorno {edisu_fmt_day(day)}: '
                                   # they have a typo in the API that appears only sometimes...
                                   f'{seats_msg["message"] or seats_msg["messsage"]}')
            valid_slots = [slot_start.split(' ', 1)[0] for slot_start in valid_slots_msg['result']['data']['list']]
            hour_start, hour_end = desired_hour_start, desired_hour_end
            if edisu_fmt_hour(hour_start) not in valid_slots:
                hour_start = edisu_parse_hour(valid_slots[0])
                logger.warning(f'Per il giorno {edisu_fmt_day(day)} l\'orario d\'inizio è {edisu_fmt_hour(hour_start)}')
            if edisu_fmt_hour(hour_end - timedelta(minutes=30)) not in valid_slots:
                hour_end = edisu_parse_hour(valid_slots[-1]) + timedelta(minutes=30)
                logger.warning(f'Per il giorno {edisu_fmt_day(day)} l\'orario di fine è {edisu_fmt_hour(hour_end)}')
            if edisu_fmt_day(day) == edisu_fmt_day(now):
                current_slot_start = timedelta(hours=now.hour, minutes=now.minute - now.minute % 30)
                if current_slot_start > hour_start:
                    hour_start = current_slot_start
                    logger.warning(f'Primo slot prenotabile oggi alle {edisu_fmt_hour(hour_start)}')
            if hour_start >= hour_end:
                logger.error(f'È tardi per prenotare oggi ({edisu_fmt_day(day)}) un posto fino alle '
                             f'{edisu_fmt_hour(hour_end)}, salto questo giorno')
                raise DaySkip()
            bookings_msg = session.post('https://edisuprenotazioni.edisu-piemonte.it:8443/sbs/web/studentbookinglist',
                                        data={'date': edisu_fmt_day(day), 'filter': -1}).json()
            logger.debug(f'/sbs/web/studentbookinglist date={edisu_fmt_day(day)}: {bookings_msg}')
            if bookings_msg.get('status', 0) != 202:
                raise RuntimeError(f'impossibile ottenere la lista prenotazioni per il giorno {edisu_fmt_day(day)}: '
                                   # they have a typo in the API that appears only sometimes...
                                   f'{bookings_msg["message"] or bookings_msg["messsage"]}')
            """
            When it is impossible to book a single shift for the desired times, you need to split the booking across
            different seats. Solve this problem by mapping it to a graph and find the shortest path between two
            vertices.
            A shift is a contiguous list of free slots in a particular seat. A shift change is a slot boundary (time
            when you may change seat). Build a graph where the vertices are the changes, and the edges are
            bookable shifts. A bookable shift (from the edisu list fetched above) can also be partially booked, with a
            starting time that can be later than the full shift start. This means that a shift of e.g. three hours maps
            to multiple edges: 1 three-hour shift, 2 two-hours-and-a-half shifts, 3 two-hours shifts, etc.
            After building the graph (in terms of a dense matrix), use Dijkstra's algorithm to find the shortest path
            (least number of changes/vertices) between the start and end change, that is hour_start and hour_end. To
            avoid booking very short shifts, solve multiple times the shortest path problem by feeding a truncated graph
            to Dijkstra's algorithm: once, by first removing the undesirable short shifts, then, if no solution is
            found, iteratively re-add the shorter shifts until a solution is found.

            TODO: scipy's Dijkstra routines only return a single solution for the shortest path between two vertices.
            If it was possible to have all the solutions, we would not need to run Dijkstra's algorithm multiple times
            with a truncated graph, but we could select the optimal solution out of the least-changes solutions.
            """
            shift_changes_n = 1 + int((hour_end - hour_start) / timedelta(minutes=30))
            change2id = dict(zip(accumulate([hour_start] + [timedelta(minutes=30)] * (shift_changes_n - 1)),
                                 range(shift_changes_n)))
            # turn keys into hour strings, '14:00' '14:30' etc.
            change2id = toolz.keymap(edisu_fmt_hour, change2id)
            id2change = dict(map(reversed, change2id.items()))
            # use a union of intervals to represent existing bookings
            already_booked_shifts = intervals.empty()
            full_requested_shift = intervals.closed(0, shift_changes_n - 1)
            for booked_shift in bookings_msg['result'].get('slots', []):
                """
                From the Android API reverse: booking status is
                0 -> Canceled
                1 -> Upcoming
                2 -> Completed
                4 -> Pending
                bitset?
                """
                if booked_shift['booking_status'] == 0 or booked_shift['hall_id'] != room_id:
                    # cancelled, expired, or other room
                    continue
                shift_start, shift_end = booked_shift['start_time'], booked_shift['end_time']
                if shift_start >= edisu_fmt_hour(hour_end) or shift_end <= edisu_fmt_hour(hour_start):
                    continue
                already_booked_shifts |= intervals.closed(change2id.get(shift_start, 0),
                                                          change2id.get(shift_end, shift_changes_n - 1))
            if (full_requested_shift - already_booked_shifts).empty:
                logger.info(f'Giorno {edisu_fmt_day(day)} già prenotato')
                raise DaySkip()
            if not already_booked_shifts.empty:
                booked_shifts_hour = [f'{id2change[interval.lower]}->{id2change[interval.upper]}'
                                      for interval in already_booked_shifts]
                logger.warning(f'Prenotazioni esistenti per il giorno {edisu_fmt_day(day)}: '
                               f'{", ".join(booked_shifts_hour)}')
            # build the full changes graph
            shift_changes_graph = ndarray((shift_changes_n, shift_changes_n), dtype=uint8)
            shift_changes_graph.fill(0)
            shift2seats = defaultdict(set)
            # keep track of the slots that cannot be booked from any seat
            unbookable_slots = full_requested_shift
            for seat in seats_msg['result']['seats']:
                for booked, shift in groupby(seat['seat'], lambda slot: int(slot['booking_status']) > 0):
                    if booked:
                        continue
                    # filter out slots that are out of hour_start and hour_end. slot_time in the JSON is the slot start
                    shift = [slot for slot in shift
                             if slot['slot_time'] in change2id and slot['slot_time'] < edisu_fmt_hour(hour_end)]
                    if not shift:
                        continue
                    change_start, change_end = change2id[shift[0]['slot_time']], change2id[shift[-1]['slot_time']] + 1
                    # add the shift as an edge to the graph, including the partial bookings
                    for i in range(change_start, change_end):
                        for j in range(i + 1, change_end + 1):
                            # remove the slots that have been booked already
                            for free_shift in intervals.closed(i, j) - already_booked_shifts:
                                if free_shift.empty:
                                    break
                                unbookable_slots -= free_shift
                                shift_changes_graph[free_shift.lower, free_shift.upper] = 1
                                shift2seats[(free_shift.lower, free_shift.upper)].add(
                                    (seat['seat_name'], seat['seat_id'])
                                )
            # do not count as unbookable the shifts that we already booked
            unbookable_slots -= already_booked_shifts
            if not unbookable_slots.empty:
                unbookable_slots_hours = [f'{id2change[interval.lower]}->{id2change[interval.upper]}'
                                          for interval in unbookable_slots]
                logger.error(f'Impossibile prenotare il periodo richiesto per il giorno {edisu_fmt_day(day)}, in '
                             f'quanto alcuni slot sono occupati su tutti i posti: {", ".join(unbookable_slots_hours)}')
                raise DaySkip()

            # run the Dijkstra algorithm with a minimum shift length
            lowest_changes_count = None
            for min_slots in range(max((shift_changes_n - 1) // 2, 1), 0, -1):
                changes_graph_pruned = shift_changes_graph.copy()
                for i in range(shift_changes_n):
                    for j in range(i + 1, min(i + min_slots, shift_changes_n)):
                        changes_graph_pruned[i, j] = 0
                # add the booked shifts in any case, because we use them to fulfill the request
                if not already_booked_shifts.empty:
                    for booked_shift in already_booked_shifts:
                        changes_graph_pruned[booked_shift.lower, booked_shift.upper] = 1
                changes_graph_pruned = csr_matrix(changes_graph_pruned, dtype=uint8)
                logger.debug(f'graph date={edisu_fmt_day(day)} min slots={min_slots}:\n{changes_graph_pruned}')
                total_shifts, predecessors = dijkstra(changes_graph_pruned, indices=0, unweighted=True,
                                                      return_predecessors=True)
                if isfinite(total_shifts[-1]):
                    # a path to the first and last vertices (hour_start and hour_end) is found!
                    lowest_changes_count = int(total_shifts[-1])
                    break
            if lowest_changes_count is None:
                # this should never happen because we computed unbookable_slots, anyway...
                logger.error(f'Algoritmo rotto nel giorno {edisu_fmt_day(day)}')
                raise DaySkip()
            # construct the shortest path from the predecessors array
            best_changes_sequence = [0, shift_changes_n - 1]
            current_change = shift_changes_n - 1
            while True:
                previous_change = predecessors[current_change]
                if previous_change == 0:
                    break
                best_changes_sequence.insert(1, previous_change)
                current_change = previous_change
            logger.debug(f'change sequence date={edisu_fmt_day(day)}: {best_changes_sequence}')

            # turn the best_changes_sequence into shifts (change pairs) and book them
            shifts_to_book = [tuple(best_changes_sequence[i:i+2]) for i in range(len(best_changes_sequence) - 1)]
            for shift in shifts_to_book:
                if intervals.closed(*shift) in already_booked_shifts:
                    continue
                available_seats = list(shift2seats[shift])
                # TODO: implement seat preference
                available_seats.sort(key=lambda s: s[0])
                my_seat = available_seats[0]
                book_msg = {'date': edisu_fmt_day(day), 'hall_id': str(room_id), 'seat_id': my_seat[1],
                            'start_time': id2change[shift[0]], 'end_time': id2change[shift[1]]}
                try:
                    print(f'prenotazione: {edisu_fmt_day(day)} {book_msg["start_time"]}->{book_msg["end_time"]} '
                          f'posto {my_seat[0]}')
                    if args.n:
                        continue
                    book_req_msg = book_session.post(
                        'https://edisuprenotazioni.edisu-piemonte.it/sbs/booking/custombooking', json=book_msg).json()
                    if book_req_msg['status'] != 202:
                        raise RuntimeError(book_req_msg['message'])
                except RuntimeError as e:
                    logger.error(f'Errore di prenotazione {edisu_fmt_day(day)} {book_msg["start_time"]}->'
                                 f'{book_msg["end_time"]} posto {my_seat[0]}: {e}')
        except RuntimeError as e:
            logger.error(f'Errore nel giorno {edisu_fmt_day(day)}: {e}')
        except DaySkip:
            pass
        day += timedelta(days=1)


if __name__ == '__main__':
    main()
