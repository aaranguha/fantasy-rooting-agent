"""Season context: does this league still matter *this week*?

A $100 league you're 2-7 in is worth less to you right now than a $20 league you
can still win.  And a league with a loser punishment inverts near the bottom -
being terrible there becomes the most urgent thing on the board.

So the static stake (buy-in x your manual multiplier) is scaled by a
**season multiplier** derived from the standings:

    effective_weight = buy_in x importance_multiplier x season_multiplier

The multiplier is the larger of two live stakes:

    UPSIDE   - how alive your title run still is
    DOWNSIDE - how likely you are to eat the punishment, ramping as the
               regular season runs out

Everything here is transparent arithmetic you can read off `fantasy-agent
standings`; nothing is hidden behind a score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .leverage import normal_cdf

# --- multiplier shape ------------------------------------------------------
UPSIDE_FLOOR = 0.30          # a mathematically dead league still gets some weight
UPSIDE_RANGE = 1.10          # 0.30 (eliminated) -> 1.40 (playoff lock)
TITLE_BONUS = 0.35           # genuine contenders get up to ~+35%
CLINCH_DAMP = 0.90           # a locked seed cares slightly less about one week
PUNISH_BASE = 0.50           # punishment urgency floor, early season
PUNISH_RAMP = 1.50           # ...climbing to 2.0x as the regular season ends
MULT_FLOOR, MULT_CEILING = 0.25, 2.50

# --- odds model ------------------------------------------------------------
WEEKLY_SD_FRACTION = 0.22    # weekly team scoring sd ~= 22% of its average
ELIMINATED_AT = 0.02
CLINCHED_AT = 0.98


@dataclass
class TeamRecord:
    team_id: str
    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0

    @property
    def games_played(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_pct(self) -> float:
        g = self.games_played
        return (self.wins + 0.5 * self.ties) / g if g else 0.5

    @property
    def effective_wins(self) -> float:
        return self.wins + 0.5 * self.ties

    @property
    def points_per_game(self) -> float:
        g = self.games_played
        return self.points_for / g if g else 0.0

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


@dataclass
class LeagueStandings:
    teams: list[TeamRecord] = field(default_factory=list)
    my_team_id: str = ""
    playoff_teams: int = 6
    regular_season_weeks: int = 14
    current_week: int = 1
    punished_places: int = 1          # how many teams eat the punishment
    error: Optional[str] = None

    @property
    def me(self) -> Optional[TeamRecord]:
        return next((t for t in self.teams if str(t.team_id) == str(self.my_team_id)), None)

    @property
    def others(self) -> list[TeamRecord]:
        return [t for t in self.teams if str(t.team_id) != str(self.my_team_id)]

    @property
    def games_remaining(self) -> int:
        """Regular-season games left, including this week."""
        return max(0, self.regular_season_weeks - self.current_week + 1)

    @property
    def urgency(self) -> float:
        """0.0 at the season opener, 1.0 once the regular season is over."""
        if self.regular_season_weeks <= 1:
            return 1.0
        done = (self.current_week - 1) / self.regular_season_weeks
        return max(0.0, min(1.0, done))

    def seed(self) -> int:
        """Current standing, ordered by record then points for."""
        ranked = sorted(self.teams,
                        key=lambda t: (-t.effective_wins, -t.points_for))
        for i, t in enumerate(ranked, 1):
            if str(t.team_id) == str(self.my_team_id):
                return i
        return len(self.teams)


@dataclass
class SeasonOutlook:
    """What's still realistically on the table in this league."""

    playoff_odds: float = 0.5
    title_odds: float = 0.1
    last_place_odds: float = 0.1
    seed: int = 1
    teams: int = 12
    record: str = "0-0"
    games_remaining: int = 0
    urgency: float = 0.0
    available: bool = True
    note: str = ""

    @property
    def eliminated(self) -> bool:
        return self.available and self.playoff_odds <= ELIMINATED_AT

    @property
    def clinched(self) -> bool:
        return self.available and self.playoff_odds >= CLINCHED_AT

    @property
    def in_playoff_position(self) -> bool:
        return self.seed <= max(1, self.teams // 2)

    def describe(self) -> str:
        if not self.available:
            return "standings unavailable"
        bits = [f"{self.record}", f"#{self.seed} of {self.teams}"]
        if self.eliminated:
            bits.append("eliminated")
        elif self.clinched:
            bits.append("playoffs clinched")
        else:
            bits.append(f"{self.playoff_odds:.0%} playoffs")
        if self.title_odds >= 0.05:
            bits.append(f"{self.title_odds:.0%} title")
        return ", ".join(bits)


# ---------------------------------------------------------------------------
# The odds model
# ---------------------------------------------------------------------------


def _finish_distribution(standings: LeagueStandings) -> dict[str, tuple[float, float]]:
    """team_id -> (expected final wins, sd of final wins).

    Each remaining game is modelled against an average opponent, using each
    team's points-per-game as its strength.  That is an approximation - we do not
    walk the remaining schedule - and it is documented as such.
    """
    teams = standings.teams
    games_left = standings.games_remaining
    played = [t for t in teams if t.games_played > 0]

    if not played:
        # Week 1: nobody has any information, so everyone is even.
        return {str(t.team_id): (games_left * 0.5, math.sqrt(games_left * 0.25))
                for t in teams}

    ppgs = [t.points_per_game for t in played]
    mean_ppg = sum(ppgs) / len(ppgs)
    weekly_sd = max(1.0, WEEKLY_SD_FRACTION * mean_ppg)
    # Margin between two independent teams in one game.
    margin_sd = weekly_sd * math.sqrt(2.0)

    out: dict[str, tuple[float, float]] = {}
    for t in teams:
        strength = t.points_per_game if t.games_played else mean_ppg
        p = normal_cdf((strength - mean_ppg) / margin_sd)
        p = min(0.95, max(0.05, p))
        expected = t.effective_wins + games_left * p
        var = games_left * p * (1.0 - p)
        out[str(t.team_id)] = (expected, math.sqrt(max(var, 1e-6)))
    return out


def _beat_probability(mine: tuple[float, float], theirs: tuple[float, float]) -> float:
    """P(I finish with more wins than this team)."""
    e_me, sd_me = mine
    e_them, sd_them = theirs
    sd = math.sqrt(sd_me ** 2 + sd_them ** 2)
    if sd < 1e-6:
        return 1.0 if e_me > e_them else (0.5 if e_me == e_them else 0.0)
    return normal_cdf((e_me - e_them) / sd)


def compute_outlook(standings: LeagueStandings) -> SeasonOutlook:
    """Playoff / title / last-place odds from the current standings."""
    me = standings.me
    if standings.error or me is None or not standings.teams:
        return SeasonOutlook(available=False, note=standings.error or "no standings",
                             teams=len(standings.teams) or 12)

    dist = _finish_distribution(standings)
    mine = dist[str(me.team_id)]
    others = sorted(
        (dist[str(t.team_id)] for t in standings.others),
        key=lambda ed: -ed[0],
    )
    n_teams = len(standings.teams)
    slots = max(1, min(standings.playoff_teams, n_teams - 1))

    # I make the playoffs iff I finish above the `slots`-th best other team.
    bubble = others[slots - 1] if len(others) >= slots else others[-1]
    playoff_odds = _beat_probability(mine, bubble)

    # I take the punishment iff I finish below the `punished_places`-th worst other.
    k = max(1, min(standings.punished_places, len(others)))
    worst = others[-k]
    last_place_odds = 1.0 - _beat_probability(mine, worst)

    # Title odds: making the field, times a strength share of the likely field.
    field = [mine] + others[: slots - 1]
    strengths = [math.exp(e / 2.0) for e, _ in field]
    share = strengths[0] / sum(strengths) if sum(strengths) else 1.0 / slots
    title_odds = playoff_odds * share

    return SeasonOutlook(
        playoff_odds=round(playoff_odds, 4),
        title_odds=round(title_odds, 4),
        last_place_odds=round(last_place_odds, 4),
        seed=standings.seed(),
        teams=n_teams,
        record=me.record,
        games_remaining=standings.games_remaining,
        urgency=round(standings.urgency, 3),
    )


# ---------------------------------------------------------------------------
# The multiplier
# ---------------------------------------------------------------------------


@dataclass
class SeasonWeight:
    multiplier: float
    reason: str
    upside: float = 1.0
    downside: float = 0.0
    outlook: Optional[SeasonOutlook] = None


def season_multiplier(outlook: SeasonOutlook, *, loser_punishment: float = 0.0,
                      enabled: bool = True) -> SeasonWeight:
    """Scale a league's stake by what's still live in it this week.

    UPSIDE grows with your playoff odds and your title odds, so a good record
    makes the league matter more and elimination makes it matter less.

    DOWNSIDE only exists in leagues you've flagged as having a loser punishment.
    It grows with your odds of finishing last AND with how late it is, so a bad
    team in a punishment league gets progressively more urgent as the playoffs
    approach - which is exactly when you can still do something about it.

    The two are combined with max(), not a sum: a team that is eliminated from
    contention but sliding toward the punishment is governed entirely by the
    downside, and vice versa.
    """
    if not enabled or not outlook.available:
        return SeasonWeight(1.0, "dynamic importance off" if not enabled
                            else "standings unavailable; using the static weight",
                            outlook=outlook)

    upside = UPSIDE_FLOOR + UPSIDE_RANGE * outlook.playoff_odds
    upside *= 1.0 + TITLE_BONUS * outlook.title_odds
    if outlook.clinched:
        upside *= CLINCH_DAMP

    downside = 0.0
    if loser_punishment > 0:
        urgency_factor = PUNISH_BASE + PUNISH_RAMP * outlook.urgency
        downside = loser_punishment * outlook.last_place_odds * urgency_factor

    raw = max(upside, downside)
    mult = round(max(MULT_FLOOR, min(MULT_CEILING, raw)), 2)

    if downside > upside:
        if outlook.urgency >= 0.6:
            reason = (f"PUNISHMENT WATCH - {outlook.last_place_odds:.0%} to finish last "
                      f"with {outlook.games_remaining} game(s) left. Do not lose this.")
        else:
            reason = (f"punishment risk - {outlook.last_place_odds:.0%} to finish last, "
                      f"but there's still time ({outlook.games_remaining} games left)")
    elif outlook.eliminated:
        reason = f"eliminated ({outlook.record}) - this league barely matters now"
    elif outlook.clinched:
        reason = f"playoffs clinched ({outlook.record}) - seeding only"
    elif outlook.playoff_odds >= 0.70:
        reason = (f"contending ({outlook.record}, {outlook.playoff_odds:.0%} playoffs, "
                  f"{outlook.title_odds:.0%} title)")
    elif outlook.playoff_odds >= 0.35:
        reason = f"on the bubble ({outlook.record}, {outlook.playoff_odds:.0%} playoffs)"
    else:
        reason = f"fading ({outlook.record}, {outlook.playoff_odds:.0%} playoffs)"

    return SeasonWeight(multiplier=mult, reason=reason, upside=round(upside, 3),
                        downside=round(downside, 3), outlook=outlook)


def apply_season_weight(league, standings: LeagueStandings, *,
                        loser_punishment: float = 0.0,
                        dynamic: bool = True) -> SeasonWeight:
    """Compute the outlook and stamp the season multiplier onto a League."""
    outlook = compute_outlook(standings)
    weight = season_multiplier(outlook, loser_punishment=loser_punishment, enabled=dynamic)
    league.season_multiplier = weight.multiplier
    league.season_note = weight.reason
    league.outlook = outlook
    return weight
