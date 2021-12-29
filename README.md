# aula-verdi
Books a seat in my favourite study room.

The program uses some reverse-engineered API to book the seats automatically. Current features:
- books a specified time shift for a specified number of days or period
- can split up a booking across different seats if a whole shift in a single seat is not available
- takes into account the own manual bookings.

Later on I will implement seat preference.

The algorithm for splitting up a shift across different seat uses [Dijkstra's algorithm](https://en.wikipedia.org/wiki/Dijkstra%27s_algorithm) on a graph where a vertex is a shift change, and the edge is a bookable shift. For more information, see the source code.

## Installation
```bash
git clone https://github.com/pisto/aula-verdi
cd aula-verdi/
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running
Here is the help message in Italian.
```
usage: main.py [-h] -l email:password (-g GG-MM-AA GG-MM-AA | -p #) -o hh:mm
               hh:mm [-e ##] [-n] [-v]

Prenota l'aula Verdi.

options:
  -h, --help            show this help message and exit
  -l email:password     Credenziali
  -g GG-MM-AA GG-MM-AA  Giorno di inizio e fine (inclusi) della prenotazione
  -p #                  Prenota a partire da oggi per i prossimi # giorni
  -o hh:mm hh:mm        Ora di inizio e fine della prenotazione (formato 24h)
  -e ##                 Giorni della settimana da escludere (ad esempio "67"
                        per sabato e domenica)
  -n                    Non effettuare la prenotazione (dry-run)
  -v                    Aumenta verbosità di logging
```
