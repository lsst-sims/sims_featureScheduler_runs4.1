__all__ = (
    "example_scheduler",
    "sched_argparser",
    "set_run_info",
    "run_sched",
    "gen_long_gaps_survey",
    "gen_greedy_surveys",
    "generate_blobs",
    "generate_twi_blobs",
    "generate_twilight_near_sun",
    "standard_bf",
)

import argparse
import os
import subprocess
import sys

import healpy as hp
import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.utils import iers

import rubin_scheduler
import rubin_scheduler.scheduler.basis_functions as bf
import rubin_scheduler.scheduler.detailers as detailers
from rubin_scheduler.scheduler import sim_runner
from rubin_scheduler.scheduler.model_observatory import ModelObservatory
from rubin_scheduler.scheduler.schedulers import CoreScheduler, SimpleFilterSched
from rubin_scheduler.scheduler.surveys import (
    BlobSurvey,
    GreedySurvey,
    LongGapSurvey,
    ScriptedSurvey,
    gen_roman_off_season,
    gen_roman_on_season,
    gen_too_surveys,
    generate_ddf_scheduled_obs,
)
from rubin_scheduler.scheduler.targetofo import gen_all_events
from rubin_scheduler.scheduler.utils import ConstantFootprint, CurrentAreaMap, make_rolling_footprints, IntRounded
from rubin_scheduler.site_models import Almanac
from rubin_scheduler.utils import DEFAULT_NSIDE, SURVEY_START_MJD, _hpid2_ra_dec

# So things don't fail on hyak
iers.conf.auto_download = False
# XXX--note this line probably shouldn't be in production
iers.conf.auto_max_age = None



class AltAzShadowMaskBasisFunction(bf.BaseBasisFunction):
    def __init__(
        self,
        nside=DEFAULT_NSIDE,
        min_alt=20.0,
        max_alt=86.5,
        min_az=0,
        max_az=360,
        shadow_minutes=40.0,
        pad=3.0,
        scale=1000,
    ):
        super().__init__(nside=nside)
        self.min_alt = np.radians(min_alt)
        self.max_alt = np.radians(max_alt)
        self.min_az = np.radians(min_az)
        self.max_az = np.radians(max_az)
        self.shadow_time = shadow_minutes / 60.0 / 24.0  # To days
        self.pad = np.radians(pad)

        self.r_min_alt = IntRounded(self.min_alt)
        self.r_max_alt = IntRounded(self.max_alt)
        self.scale = scale

    def _calc_value(self, conditions, indx=None):
        result = np.zeros(hp.nside2npix(self.nside), dtype=float)

        alt_ir = IntRounded(conditions.alt)
        oob = np.where(alt_ir > self.r_max_alt)[0]
        result[oob] = np.nan

        oob = np.where(alt_ir < self.r_min_alt)[0]
        result[oob] = np.nan
        return result


def example_scheduler(
    nside: int = DEFAULT_NSIDE, mjd_start: float = SURVEY_START_MJD, no_too: bool = False
) -> CoreScheduler:
    """Provide an example baseline survey-strategy scheduler.

    Parameters
    ----------
    nside : `int`
        Nside for the scheduler maps and basis functions.
    mjd_start : `float`
        Start date for the survey (MJD).
    no_too : `bool`
        Turn off ToO simulation. Default False.

    Returns
    -------
    scheduler : `rubin_scheduler.scheduler.CoreScheduler`
        A scheduler set up as the baseline survey strategy.
    """
    parser = sched_argparser()
    args = parser.parse_args(args=[])
    args.setup_only = True
    args.no_too = no_too
    args.dbroot = "example_"
    args.outDir = "."
    args.nside = nside
    args.mjd_start = mjd_start
    scheduler = gen_scheduler(args)
    return scheduler


def standard_bf(
    nside,
    filtername="g",
    filtername2="i",
    m5_weight=6.0,
    footprint_weight=1.5,
    slewtime_weight=3.0,
    stayfilter_weight=3.0,
    template_weight=12.0,
    u_template_weight=50.0,
    g_template_weight=50.0,
    footprints=None,
    n_obs_template=None,
    season=300.0,
    season_start_hour=-4.0,
    season_end_hour=2.0,
    moon_distance=30.0,
    strict=True,
    wind_speed_maximum=20.0,
):
    """Generate the standard basis functions that are shared by blob surveys

    Parameters
    ----------
    nside : int (DEFAULT_NSIDE)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filtername : list of str
        The filternames for the first set
    filtername2 : list of str
        The filter names for the second in the pair (None if unpaired)
    n_obs_template : dict (None)
        The number of observations to take every season in each filter
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be
        observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to be
        observed (hours)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    u_template_weight : float (24.)
        The weight to place on getting image templates in u-band. Since there
        are so few u-visits, it can be helpful to turn this up a little
        higher than the standard template_weight kwarg.
    g_template_weight : float (24.)
        The weight to place on getting image templates in g-band. Since there
        are so few g-visits, it can be helpful to turn this up a
        little higher than the standard template_weight kwarg.

    Returns
    -------
    basis_functions_weights : `list`
        list of tuple pairs (basis function, weight) that is
        (rubin_scheduler.scheduler.BasisFunction object, float)

    """
    template_weights = {
        "u": u_template_weight,
        "g": g_template_weight,
        "r": template_weight,
        "i": template_weight,
        "z": template_weight,
        "y": template_weight,
    }

    bfs = []

    if filtername2 is not None:
        bfs.append(
            (
                bf.M5DiffBasisFunction(filtername=filtername, nside=nside),
                m5_weight / 2.0,
            )
        )
        bfs.append(
            (
                bf.M5DiffBasisFunction(filtername=filtername2, nside=nside),
                m5_weight / 2.0,
            )
        )

    else:
        bfs.append((bf.M5DiffBasisFunction(filtername=filtername, nside=nside), m5_weight))

    if filtername2 is not None:
        bfs.append(
            (
                bf.FootprintBasisFunction(
                    filtername=filtername,
                    footprint=footprints,
                    out_of_bounds_val=np.nan,
                    nside=nside,
                ),
                footprint_weight / 2.0,
            )
        )
        bfs.append(
            (
                bf.FootprintBasisFunction(
                    filtername=filtername2,
                    footprint=footprints,
                    out_of_bounds_val=np.nan,
                    nside=nside,
                ),
                footprint_weight / 2.0,
            )
        )
    else:
        bfs.append(
            (
                bf.FootprintBasisFunction(
                    filtername=filtername,
                    footprint=footprints,
                    out_of_bounds_val=np.nan,
                    nside=nside,
                ),
                footprint_weight,
            )
        )

    bfs.append(
        (
            bf.SlewtimeBasisFunction(filtername=filtername, nside=nside),
            slewtime_weight,
        )
    )
    if strict:
        bfs.append((bf.StrictFilterBasisFunction(filtername=filtername), stayfilter_weight))
    else:
        bfs.append((bf.FilterChangeBasisFunction(filtername=filtername), stayfilter_weight))

    if n_obs_template is not None:
        if filtername2 is not None:
            bfs.append(
                (
                    bf.NObsPerYearBasisFunction(
                        filtername=filtername,
                        nside=nside,
                        footprint=footprints.get_footprint(filtername),
                        n_obs=n_obs_template[filtername],
                        season=season,
                        season_start_hour=season_start_hour,
                        season_end_hour=season_end_hour,
                    ),
                    template_weights[filtername] / 2.0,
                )
            )
            bfs.append(
                (
                    bf.NObsPerYearBasisFunction(
                        filtername=filtername2,
                        nside=nside,
                        footprint=footprints.get_footprint(filtername2),
                        n_obs=n_obs_template[filtername2],
                        season=season,
                        season_start_hour=season_start_hour,
                        season_end_hour=season_end_hour,
                    ),
                    template_weights[filtername2] / 2.0,
                )
            )
        else:
            bfs.append(
                (
                    bf.NObsPerYearBasisFunction(
                        filtername=filtername,
                        nside=nside,
                        footprint=footprints.get_footprint(filtername),
                        n_obs=n_obs_template[filtername],
                        season=season,
                        season_start_hour=season_start_hour,
                        season_end_hour=season_end_hour,
                    ),
                    template_weights[filtername],
                )
            )

    # The shared masks
    bfs.append(
        (
            bf.MoonAvoidanceBasisFunction(nside=nside, moon_distance=moon_distance),
            0.0,
        )
    )
    bfs.append((bf.AvoidDirectWind(nside=nside, wind_speed_maximum=wind_speed_maximum), 0))
    filternames = [fn for fn in [filtername, filtername2] if fn is not None]
    bfs.append((bf.FilterLoadedBasisFunction(filternames=filternames), 0))
    bfs.append((bf.PlanetMaskBasisFunction(nside=nside), 0.0))

    return bfs


def blob_for_long(
    nside,
    nexp=2,
    exptime=29.2,
    filter1s=["g"],
    filter2s=["i"],
    pair_time=33.0,
    camera_rot_limits=[-80.0, 80.0],
    n_obs_template=None,
    season=300.0,
    season_start_hour=-4.0,
    season_end_hour=2.0,
    shadow_minutes=60.0,
    max_alt=76.0,
    moon_distance=30.0,
    ignore_obs=["DD", "twilight_near_sun"],
    m5_weight=6.0,
    footprint_weight=1.5,
    slewtime_weight=3.0,
    stayfilter_weight=3.0,
    template_weight=12.0,
    u_template_weight=50.0,
    g_template_weight=50.0,
    footprints=None,
    u_nexp1=True,
    night_pattern=[True, True],
    time_after_twi=30.0,
    HA_min=12,
    HA_max=24 - 3.5,
    blob_names=[],
    u_exptime=38.0,
    scheduled_respect=30.0,
):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int (DEFAULT_NSIDE)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (33)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : dict (None)
        The number of observations to take every season in each filter.
        If None, sets to 3 each.
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be
        observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to be
        observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    u_template_weight : float (24.)
        The weight to place on getting image templates in u-band. Since there
        are so few u-visits, it can be helpful to turn this up a
        little higher than the standard template_weight kwarg.
    u_nexp1 : bool (True)
        Add a detailer to make sure the number of expossures
        in a visit is always 1 for u observations.
    """

    BlobSurvey_params = {
        "slew_approx": 7.5,
        "filter_change_approx": 140.0,
        "read_approx": 2.0,
        "flush_time": 30.0,
        "smoothing_kernel": None,
        "nside": nside,
        "seed": 42,
        "dither": True,
        "twilight_scale": True,
    }

    surveys = []
    if n_obs_template is None:
        n_obs_template = {"u": 3, "g": 3, "r": 3, "i": 3, "z": 3, "y": 3}

    times_needed = [pair_time, pair_time * 2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(
            detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
        )
        detailer_list.append(detailers.CloseAltDetailer())
        detailer_list.append(detailers.FilterNexp(filtername="u", nexp=1, exptime=u_exptime))

        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        bfs.extend(
            standard_bf(
                nside,
                filtername=filtername,
                filtername2=filtername2,
                m5_weight=m5_weight,
                footprint_weight=footprint_weight,
                slewtime_weight=slewtime_weight,
                stayfilter_weight=stayfilter_weight,
                template_weight=template_weight,
                u_template_weight=u_template_weight,
                g_template_weight=g_template_weight,
                footprints=footprints,
                n_obs_template=n_obs_template,
                season=season,
                season_start_hour=season_start_hour,
                season_end_hour=season_end_hour,
            )
        )

        # Make sure we respect scheduled observations
        bfs.append((bf.TimeToScheduledBasisFunction(time_needed=scheduled_respect), 0))

        # Masks, give these 0 weight
        bfs.append(
            (
                AltAzShadowMaskBasisFunction(
                    nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt, pad=3.0
                ),
                0.0,
            )
        )
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.TimeToTwilightBasisFunction(time_needed=time_needed), 0.0))
        bfs.append((bf.NotTwilightBasisFunction(), 0.0))
        bfs.append((bf.AfterEveningTwiBasisFunction(time_after=time_after_twi), 0.0))
        bfs.append((bf.HaMaskBasisFunction(ha_min=HA_min, ha_max=HA_max, nside=nside), 0.0))
        # don't execute every night
        bfs.append((bf.NightModuloBasisFunction(night_pattern), 0.0))
        # only execute one blob per night
        bfs.append((bf.OnceInNightBasisFunction(notes=blob_names), 0))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = "blob_long, %s" % filtername
        else:
            survey_name = "blob_long, %s%s" % (filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.TakeAsPairsDetailer(filtername=filtername2))

        if u_nexp1:
            detailer_list.append(detailers.FilterNexp(filtername="u", nexp=1))
        surveys.append(
            BlobSurvey(
                basis_functions,
                weights,
                filtername1=filtername,
                filtername2=filtername2,
                exptime=exptime,
                ideal_pair_time=pair_time,
                survey_name=survey_name,
                ignore_obs=ignore_obs,
                nexp=nexp,
                detailers=detailer_list,
                **BlobSurvey_params,
            )
        )

    return surveys


def gen_long_gaps_survey(
    footprints,
    nside=DEFAULT_NSIDE,
    night_pattern=[True, True],
    gap_range=[2, 7],
    HA_min=12,
    HA_max=24 - 3.5,
    time_after_twi=120,
    u_template_weight=50.0,
    g_template_weight=50.0,
    u_exptime=38.0,
    nexp=2,
):
    """
    Paramterers
    -----------
    HA_min(_max) : float
        The hour angle limits passed to the initial blob scheduler.
    """

    surveys = []
    f1 = ["g", "r", "i"]
    f2 = ["r", "i", "z"]
    # Maybe force scripted to not go in twilight?
    blob_names = []
    for fn1, fn2 in zip(f1, f2):
        for ab in ["a", "b"]:
            blob_names.append("blob_long, %s%s, %s" % (fn1, fn2, ab))
    for filtername1, filtername2 in zip(f1, f2):
        blob = blob_for_long(
            footprints=footprints,
            nside=nside,
            filter1s=[filtername1],
            filter2s=[filtername2],
            night_pattern=night_pattern,
            time_after_twi=time_after_twi,
            HA_min=HA_min,
            HA_max=HA_max,
            u_template_weight=u_template_weight,
            g_template_weight=g_template_weight,
            blob_names=blob_names,
            u_exptime=u_exptime,
            nexp=nexp,
        )
        scripted = ScriptedSurvey(
            [bf.AvoidDirectWind(nside=nside)],
            nside=nside,
            ignore_obs=["blob", "DDF", "twi", "pair"],
        )
        surveys.append(LongGapSurvey(blob[0], scripted, gap_range=gap_range, avoid_zenith=True))

    return surveys


def gen_greedy_surveys(
    nside=DEFAULT_NSIDE,
    nexp=2,
    exptime=29.2,
    filters=["r", "i", "z", "y"],
    camera_rot_limits=[-80.0, 80.0],
    shadow_minutes=0.0,
    max_alt=76.0,
    moon_distance=30.0,
    ignore_obs=["DD", "twilight_near_sun"],
    m5_weight=3.0,
    footprint_weight=0.75,
    slewtime_weight=3.0,
    stayfilter_weight=100.0,
    repeat_weight=-1.0,
    footprints=None,
):
    """
    Make a quick set of greedy surveys

    This is a convenience function to generate a list of survey objects
    that can be used with
    rubin_scheduler.scheduler.schedulers.Core_scheduler.
    To ensure we are robust against changes in the sims_featureScheduler
    codebase, all kwargs are
    explicitly set.

    Parameters
    ----------
    nside : int (DEFAULT_NSIDE)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filters : list of str (['r', 'i', 'z', 'y'])
        Which filters to generate surveys for.
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    """
    # Define the extra parameters that are used in the greedy survey. I
    # think these are fairly set, so no need to promote to utility func kwargs
    greed_survey_params = {
        "block_size": 1,
        "smoothing_kernel": None,
        "seed": 42,
        "camera": "LSST",
        "dither": True,
        "survey_name": "greedy",
    }

    surveys = []
    detailer_list = [
        detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
    ]
    detailer_list.append(detailers.Rottep2RotspDesiredDetailer())

    for filtername in filters:
        bfs = []
        bfs.extend(
            standard_bf(
                nside,
                filtername=filtername,
                filtername2=None,
                m5_weight=m5_weight,
                footprint_weight=footprint_weight,
                slewtime_weight=slewtime_weight,
                stayfilter_weight=stayfilter_weight,
                template_weight=0,
                u_template_weight=0,
                g_template_weight=0,
                footprints=footprints,
                n_obs_template=None,
                strict=False,
            )
        )

        bfs.append(
            (
                bf.VisitRepeatBasisFunction(
                    gap_min=0, gap_max=2 * 60.0, filtername=None, nside=nside, npairs=20
                ),
                repeat_weight,
            )
        )
        # Masks, give these 0 weight
        bfs.append(
            (
                AltAzShadowMaskBasisFunction(
                    nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt, pad=3.0
                ),
                0,
            )
        )
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        surveys.append(
            GreedySurvey(
                basis_functions,
                weights,
                exptime=exptime,
                filtername=filtername,
                nside=nside,
                ignore_obs=ignore_obs,
                nexp=nexp,
                detailers=detailer_list,
                **greed_survey_params,
            )
        )

    return surveys


def generate_blobs(
    nside,
    nexp=2,
    exptime=29.2,
    filter1s=["u", "u", "g", "r", "i", "z", "y"],
    filter2s=["g", "r", "r", "i", "z", "y", "y"],
    pair_time=33.0,
    camera_rot_limits=[-80.0, 80.0],
    n_obs_template=None,
    season=300.0,
    season_start_hour=-4.0,
    season_end_hour=2.0,
    shadow_minutes=60.0,
    max_alt=76.0,
    moon_distance=30.0,
    ignore_obs=["DD", "twilight_near_sun"],
    m5_weight=6.0,
    footprint_weight=1.5,
    slewtime_weight=3.0,
    stayfilter_weight=3.0,
    template_weight=12.0,
    u_template_weight=50.0,
    g_template_weight=50.0,
    footprints=None,
    u_nexp1=True,
    scheduled_respect=45.0,
    good_seeing={"g": 3, "r": 3, "i": 3},
    good_seeing_weight=3.0,
    mjd_start=1,
    repeat_weight=-20,
    u_exptime=38.0,
):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (33)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : Dict (None)
        The number of observations to take every season in each filter.
        If None, sets to 3 each.
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be
        observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to
        be observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    u_template_weight : float (24.)
        The weight to place on getting image templates in u-band. Since there
        are so few u-visits, it can be helpful to turn this up a
        little higher than the standard template_weight kwarg.
    u_nexp1 : bool (True)
        Add a detailer to make sure the number of expossures in a visit
        is always 1 for u observations.
    scheduled_respect : float (45)
        How much time to require there be before a pre-scheduled
        observation (minutes)
    """

    BlobSurvey_params = {
        "slew_approx": 7.5,
        "filter_change_approx": 140.0,
        "read_approx": 2.0,
        "flush_time": 30.0,
        "smoothing_kernel": None,
        "nside": nside,
        "seed": 42,
        "dither": True,
        "twilight_scale": False,
    }

    if n_obs_template is None:
        n_obs_template = {"u": 3, "g": 3, "r": 3, "i": 3, "z": 3, "y": 3}

    surveys = []

    times_needed = [pair_time, pair_time * 2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(
            detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
        )
        detailer_list.append(detailers.Rottep2RotspDesiredDetailer())
        detailer_list.append(detailers.CloseAltDetailer())
        detailer_list.append(detailers.FlushForSchedDetailer())
        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        bfs.extend(
            standard_bf(
                nside,
                filtername=filtername,
                filtername2=filtername2,
                m5_weight=m5_weight,
                footprint_weight=footprint_weight,
                slewtime_weight=slewtime_weight,
                stayfilter_weight=stayfilter_weight,
                template_weight=template_weight,
                u_template_weight=u_template_weight,
                g_template_weight=g_template_weight,
                footprints=footprints,
                n_obs_template=n_obs_template,
                season=season,
                season_start_hour=season_start_hour,
                season_end_hour=season_end_hour,
            )
        )

        bfs.append(
            (
                bf.VisitRepeatBasisFunction(
                    gap_min=0, gap_max=3 * 60.0, filtername=None, nside=nside, npairs=20
                ),
                repeat_weight,
            )
        )

        # Insert things for getting good seeing templates
        if filtername2 is not None:
            if filtername in list(good_seeing.keys()):
                bfs.append(
                    (
                        bf.NGoodSeeingBasisFunction(
                            filtername=filtername,
                            nside=nside,
                            mjd_start=mjd_start,
                            footprint=footprints.get_footprint(filtername),
                            n_obs_desired=good_seeing[filtername],
                        ),
                        good_seeing_weight,
                    )
                )
            if filtername2 in list(good_seeing.keys()):
                bfs.append(
                    (
                        bf.NGoodSeeingBasisFunction(
                            filtername=filtername2,
                            nside=nside,
                            mjd_start=mjd_start,
                            footprint=footprints.get_footprint(filtername2),
                            n_obs_desired=good_seeing[filtername2],
                        ),
                        good_seeing_weight,
                    )
                )
        else:
            if filtername in list(good_seeing.keys()):
                bfs.append(
                    (
                        bf.NGoodSeeingBasisFunction(
                            filtername=filtername,
                            nside=nside,
                            mjd_start=mjd_start,
                            footprint=footprints.get_footprint(filtername),
                            n_obs_desired=good_seeing[filtername],
                        ),
                        good_seeing_weight,
                    )
                )
        # Make sure we respect scheduled observations
        bfs.append((bf.TimeToScheduledBasisFunction(time_needed=scheduled_respect), 0))
        # Masks, give these 0 weight
        bfs.append(
            (
                AltAzShadowMaskBasisFunction(
                    nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt, pad=3.0
                ),
                0.0,
            )
        )
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.TimeToTwilightBasisFunction(time_needed=time_needed), 0.0))
        bfs.append((bf.NotTwilightBasisFunction(), 0.0))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = "pair_%i, %s" % (pair_time, filtername)
        else:
            survey_name = "pair_%i, %s%s" % (pair_time, filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.TakeAsPairsDetailer(filtername=filtername2))

        if u_nexp1:
            detailer_list.append(detailers.FilterNexp(filtername="u", nexp=1, exptime=u_exptime))
        surveys.append(
            BlobSurvey(
                basis_functions,
                weights,
                filtername1=filtername,
                filtername2=filtername2,
                exptime=exptime,
                ideal_pair_time=pair_time,
                survey_name=survey_name,
                ignore_obs=ignore_obs,
                nexp=nexp,
                detailers=detailer_list,
                **BlobSurvey_params,
            )
        )

    return surveys


def generate_twi_blobs(
    nside,
    nexp=2,
    exptime=29.2,
    filter1s=["r", "i", "z", "y"],
    filter2s=["i", "z", "y", "y"],
    pair_time=15.0,
    camera_rot_limits=[-80.0, 80.0],
    n_obs_template=None,
    season=300.0,
    season_start_hour=-4.0,
    season_end_hour=2.0,
    shadow_minutes=60.0,
    max_alt=76.0,
    moon_distance=30.0,
    ignore_obs=["DD", "twilight_near_sun"],
    m5_weight=6.0,
    footprint_weight=1.5,
    slewtime_weight=3.0,
    stayfilter_weight=3.0,
    template_weight=12.0,
    footprints=None,
    repeat_night_weight=None,
    wfd_footprint=None,
    scheduled_respect=15.0,
    repeat_weight=-1.0,
    night_pattern=None,
):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (22)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : dict (None)
        The number of observations to take every season in each filter.
        If None, sets to 3 each.
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be
        observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to
        be observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    u_template_weight : float (24.)
        The weight to place on getting image templates in u-band.
        Since there are so few u-visits, it can be helpful to turn
        this up a little higher than the standard template_weight kwarg.
    """

    BlobSurvey_params = {
        "slew_approx": 7.5,
        "filter_change_approx": 140.0,
        "read_approx": 2.0,
        "flush_time": 30.0,
        "smoothing_kernel": None,
        "nside": nside,
        "seed": 42,
        "dither": True,
        "twilight_scale": False,
        "in_twilight": True,
    }

    surveys = []

    if n_obs_template is None:
        n_obs_template = {"u": 3, "g": 3, "r": 3, "i": 3, "z": 3, "y": 3}

    times_needed = [pair_time, pair_time * 2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(
            detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
        )
        detailer_list.append(detailers.Rottep2RotspDesiredDetailer())
        detailer_list.append(detailers.CloseAltDetailer())
        detailer_list.append(detailers.FlushForSchedDetailer())
        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        bfs.extend(
            standard_bf(
                nside,
                filtername=filtername,
                filtername2=filtername2,
                m5_weight=m5_weight,
                footprint_weight=footprint_weight,
                slewtime_weight=slewtime_weight,
                stayfilter_weight=stayfilter_weight,
                template_weight=template_weight,
                u_template_weight=0,
                g_template_weight=0,
                footprints=footprints,
                n_obs_template=n_obs_template,
                season=season,
                season_start_hour=season_start_hour,
                season_end_hour=season_end_hour,
            )
        )

        bfs.append(
            (
                bf.VisitRepeatBasisFunction(
                    gap_min=0, gap_max=2 * 60.0, filtername=None, nside=nside, npairs=20
                ),
                repeat_weight,
            )
        )

        if repeat_night_weight is not None:
            bfs.append(
                (
                    bf.AvoidLongGapsBasisFunction(
                        nside=nside,
                        filtername=None,
                        min_gap=0.0,
                        max_gap=10.0 / 24.0,
                        ha_limit=3.5,
                        footprint=wfd_footprint,
                    ),
                    repeat_night_weight,
                )
            )
        # Make sure we respect scheduled observations
        bfs.append((bf.TimeToScheduledBasisFunction(time_needed=scheduled_respect), 0))
        # Masks, give these 0 weight
        bfs.append(
            (
                AltAzShadowMaskBasisFunction(
                    nside=nside,
                    shadow_minutes=shadow_minutes,
                    max_alt=max_alt,
                    pad=3.0,
                ),
                0.0,
            )
        )
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.TimeToTwilightBasisFunction(time_needed=time_needed, alt_limit=12), 0.0))

        # Let's turn off twilight blobs on nights where we are
        # doing NEO hunts
        bfs.append((bf.NightModuloBasisFunction(pattern=night_pattern), 0))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = "pair_%i, %s" % (pair_time, filtername)
        else:
            survey_name = "pair_%i, %s%s" % (pair_time, filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.TakeAsPairsDetailer(filtername=filtername2))
        surveys.append(
            BlobSurvey(
                basis_functions,
                weights,
                filtername1=filtername,
                filtername2=filtername2,
                exptime=exptime,
                ideal_pair_time=pair_time,
                survey_name=survey_name,
                ignore_obs=ignore_obs,
                nexp=nexp,
                detailers=detailer_list,
                **BlobSurvey_params,
            )
        )

    return surveys


def ddf_surveys(
    detailers=None,
    season_unobs_frac=0.2,
    euclid_detailers=None,
    nside=None,
    expt=29.2,
    nexp=2,
):
    nsnaps = [1, 2, 2, 2, 2, 2]
    if nexp == 1:
        nsnaps = [1, 1, 1, 1, 1, 1]
    obs_array = generate_ddf_scheduled_obs(season_unobs_frac=season_unobs_frac, expt=expt, nsnaps=nsnaps)
    euclid_obs = np.where(
        (obs_array["scheduler_note"] == "DD:EDFS_b") | (obs_array["scheduler_note"] == "DD:EDFS_a")
    )[0]
    all_other = np.where(
        (obs_array["scheduler_note"] != "DD:EDFS_b") & (obs_array["scheduler_note"] != "DD:EDFS_a")
    )[0]

    survey1 = ScriptedSurvey([bf.AvoidDirectWind(nside=nside)], nside=nside, detailers=detailers)
    survey1.set_script(obs_array[all_other])

    survey2 = ScriptedSurvey([bf.AvoidDirectWind(nside=nside)], nside=nside, detailers=euclid_detailers)
    survey2.set_script(obs_array[euclid_obs])

    return [survey1, survey2]


def ecliptic_target(nside=DEFAULT_NSIDE, dist_to_eclip=40.0, dec_max=30.0, mask=None):
    """Generate a target_map for the area around the ecliptic

    Parameters
    ----------
    nside : int
        The HEALpix nside to use
    dist_to_eclip : float (40)
        The distance to the ecliptic to constrain to (degrees).
    dec_max : float (30)
        The max declination to alow (degrees).
    mask : np.array (None)
        Any additional mask to apply, should be a
        HEALpix mask with matching nside.
    """

    ra, dec = _hpid2_ra_dec(nside, np.arange(hp.nside2npix(nside)))
    result = np.zeros(ra.size)
    coord = SkyCoord(ra=ra * u.rad, dec=dec * u.rad)
    eclip_lat = coord.barycentrictrueecliptic.lat.radian
    good = np.where((np.abs(eclip_lat) < np.radians(dist_to_eclip)) & (dec < np.radians(dec_max)))
    result[good] += 1

    if mask is not None:
        result *= mask

    return result


def generate_twilight_near_sun(
    nside,
    night_pattern=None,
    nexp=1,
    exptime=15,
    ideal_pair_time=5.0,
    max_airmass=2.0,
    camera_rot_limits=[-80.0, 80.0],
    time_needed=10,
    footprint_mask=None,
    footprint_weight=0.1,
    slewtime_weight=3.0,
    stayfilter_weight=3.0,
    min_area=None,
    filters="riz",
    n_repeat=4,
    sun_alt_limit=-14.8,
    slew_estimate=4.5,
    moon_distance=30.0,
    shadow_minutes=0,
    min_alt=20.0,
    max_alt=76.0,
    max_elong=60.0,
    ignore_obs=["DD", "pair", "long", "blob", "greedy"],
    filter_dist_weight=0.3,
    time_to_12deg=25.0,
):
    """Generate a survey for observing NEO objects in twilight

    Parameters
    ----------
    night_pattern : list of bool (None)
        A list of bools that set when the survey will be
        active. e.g., [True, False] for every-other night,
        [True, False, False] for every third night.
    nexp : int (1)
        Number of snaps in a visit
    exptime : float (15)
        Exposure time of visits
    ideal_pair_time : float (5)
        Ideal time between repeat visits (minutes).
    max_airmass : float (2)
        Maximum airmass to attempt (unitless).
    camera_rot_limits : list of float ([-80, 80])
        The camera rotation limits to use (degrees).
    time_needed : float (10)
        How much time should be available
        (e.g., before twilight ends) (minutes).
    footprint_mask : np.array (None)
        Mask to apply to the constructed ecliptic target mask (None).
    footprint_weight : float (0.1)
        Weight for footprint basis function
    slewtime_weight : float (3.)
        Weight for slewtime basis function
    stayfilter_weight : float (3.)
        Weight for staying in the same filter basis function
    min_area : float (None)
        The area that needs to be available before the survey will return
        observations (sq degrees?)
    filters : str ('riz')
        The filters to use, default 'riz'
    n_repeat : int (4)
        The number of times a blob should be repeated, default 4.
    sun_alt_limit : float (-14.8)
        Do not start unless sun is higher than this limit (degrees)
    slew_estimate : float (4.5)
        An estimate of how long it takes to slew between
        neighboring fields (seconds).
    time_to_sunrise : float (25.)
        Do not execute if time to sunrise is greater than (minutes).
    """
    survey_name = "twilight_near_sun"
    footprint = ecliptic_target(nside=nside, mask=footprint_mask)
    constant_fp = ConstantFootprint(nside=nside)
    for filtername in filters:
        constant_fp.set_footprint(filtername, footprint)

    surveys = []
    for filtername in filters:
        detailer_list = []
        detailer_list.append(
            detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
        )
        detailer_list.append(detailers.CloseAltDetailer())
        # Should put in a detailer so things start at lowest altitude
        detailer_list.append(detailers.TwilightTripleDetailer(slew_estimate=slew_estimate, n_repeat=n_repeat))
        detailer_list.append(detailers.RandomFilterDetailer(filters=filters))
        bfs = []

        bfs.append(
            (
                bf.FootprintBasisFunction(
                    filtername=filtername,
                    footprint=constant_fp,
                    out_of_bounds_val=np.nan,
                    nside=nside,
                ),
                footprint_weight,
            )
        )

        bfs.append(
            (
                bf.SlewtimeBasisFunction(filtername=filtername, nside=nside),
                slewtime_weight,
            )
        )
        bfs.append((bf.StrictFilterBasisFunction(filtername=filtername), stayfilter_weight))
        bfs.append((bf.FilterDistBasisFunction(filtername=filtername), filter_dist_weight))
        # Need a toward the sun, reward high airmass, with an
        # airmass cutoff basis function.
        bfs.append(
            (
                bf.NearSunHighAirmassBasisFunction(nside=nside, max_airmass=max_airmass),
                0,
            )
        )
        bfs.append(
            (
                AltAzShadowMaskBasisFunction(
                    nside=nside,
                    shadow_minutes=shadow_minutes,
                    max_alt=max_alt,
                    min_alt=min_alt,
                    pad=3.0,
                ),
                0,
            )
        )
        bfs.append((bf.MoonAvoidanceBasisFunction(nside=nside, moon_distance=moon_distance), 0))
        bfs.append((bf.FilterLoadedBasisFunction(filternames=filtername), 0))
        bfs.append((bf.PlanetMaskBasisFunction(nside=nside), 0))
        bfs.append(
            (
                bf.SolarElongationMaskBasisFunction(min_elong=0.0, max_elong=max_elong, nside=nside),
                0,
            )
        )

        bfs.append((bf.NightModuloBasisFunction(pattern=night_pattern), 0))
        # Do not attempt unless the sun is getting high
        bfs.append(
            (
                (
                    bf.CloseToTwilightBasisFunction(
                        max_sun_alt_limit=sun_alt_limit, max_time_to_12deg=time_to_12deg
                    )
                ),
                0,
            )
        )

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]

        # Set huge ideal pair time and use the detailer to cut down
        # the list of observations to fit twilight?
        surveys.append(
            BlobSurvey(
                basis_functions,
                weights,
                filtername1=filtername,
                filtername2=None,
                ideal_pair_time=ideal_pair_time,
                nside=nside,
                exptime=exptime,
                survey_name=survey_name,
                ignore_obs=ignore_obs,
                dither=True,
                nexp=nexp,
                detailers=detailer_list,
                twilight_scale=False,
                min_area=min_area,
            )
        )
    return surveys


def set_run_info(dbroot=None, file_end="v3.4_", out_dir="."):
    """Gather versions of software used to record"""
    extra_info = {}
    exec_command = ""
    for arg in sys.argv:
        exec_command += " " + arg
    extra_info["exec command"] = exec_command
    try:
        extra_info["git hash"] = subprocess.check_output(["git", "rev-parse", "HEAD"])
    except subprocess.CalledProcessError:
        extra_info["git hash"] = "Not in git repo"

    extra_info["file executed"] = os.path.realpath(__file__)
    try:
        rs_path = rubin_scheduler.__path__[0]
        hash_file = os.path.join(rs_path, "../", ".git/refs/heads/main")
        extra_info["rubin_scheduler git hash"] = subprocess.check_output(["cat", hash_file])
    except subprocess.CalledProcessError:
        pass

    # Use the filename of the script to name the output database
    if dbroot is None:
        fileroot = os.path.basename(sys.argv[0]).replace(".py", "") + "_"
    else:
        fileroot = dbroot + "_"
    fileroot = os.path.join(out_dir, fileroot + file_end)
    return fileroot, extra_info


def run_sched(
    scheduler,
    survey_length=365.25,
    nside=DEFAULT_NSIDE,
    filename=None,
    verbose=False,
    extra_info=None,
    illum_limit=40.0,
    mjd_start=60796.0,
    event_table=None,
    sim_to_o=None,
):
    """Run survey"""
    n_visit_limit = None
    fs = SimpleFilterSched(illum_limit=illum_limit)
    observatory = ModelObservatory(nside=nside, mjd_start=mjd_start, sim_to_o=sim_to_o)
    observatory, scheduler, observations = sim_runner(
        observatory,
        scheduler,
        sim_duration=survey_length,
        filename=filename,
        delete_past=True,
        n_visit_limit=n_visit_limit,
        verbose=verbose,
        extra_info=extra_info,
        filter_scheduler=fs,
        event_table=event_table,
    )

    return observatory, scheduler, observations


def gen_scheduler(args):
    survey_length = args.survey_length  # Days
    out_dir = args.out_dir
    verbose = args.verbose
    nexp = args.nexp
    dbroot = args.dbroot
    nside = args.nside
    mjd_plus = args.mjd_plus
    split_long = args.split_long
    too = ~args.no_too

    # Parameters that were previously command-line
    # arguments.
    max_dither = 0.2  # Degrees. For DDFs
    ddf_season_frac = 0.2  # Amount of season to use for DDFs
    illum_limit = 40.0  # Percent. Lunar illumination used for filter loading
    u_exptime = 38.0  # Deconds
    nslice = 2  # N slices for rolling
    rolling_scale = 0.9  # Strength of rolling
    rolling_uniform = True  # Should we use the uniform rolling flag
    nights_off = 3  # For long gaps
    ei_night_pattern = 4  # select doing earth interior observation every 4 nights
    ei_filters = "riz"  # Filters to use for earth interior observations.
    ei_repeat = 4  # Number of times to repeat earth interior observations
    ei_am = 2.5  # Earth interior airmass limit
    ei_elong_req = 45.0  # Solar elongation required for inner solar system
    ei_area_req = 0.0  # Sky area required before attempting inner solar system
    per_night = True  # Dither DDF per night
    camera_ddf_rot_limit = 75.0  # degrees

    # Be sure to also update and regenerate DDF grid save file
    # if changing mjd_start
    mjd_start = SURVEY_START_MJD + mjd_plus

    fileroot, extra_info = set_run_info(dbroot=dbroot, file_end="v4.1_", out_dir=out_dir)

    pattern_dict = {
        1: [True],
        2: [True, False],
        3: [True, False, False],
        4: [True, False, False, False],
        # 4 on, 4 off
        5: [True, True, True, True, False, False, False, False],
        # 3 on 4 off
        6: [True, True, True, False, False, False, False],
        7: [True, True, False, False, False, False],
    }
    ei_night_pattern = pattern_dict[ei_night_pattern]
    reverse_ei_night_pattern = [not val for val in ei_night_pattern]

    sky = CurrentAreaMap(nside=nside)
    footprints_hp_array, labels = sky.return_maps()

    wfd_indx = np.where((labels == "lowdust") | (labels == "virgo"))[0]
    wfd_footprint = footprints_hp_array["r"] * 0
    wfd_footprint[wfd_indx] = 1

    footprints_hp = {}
    for key in footprints_hp_array.dtype.names:
        footprints_hp[key] = footprints_hp_array[key]

    footprint_mask = footprints_hp["r"] * 0
    footprint_mask[np.where(footprints_hp["r"] > 0)] = 1

    repeat_night_weight = None

    # Use the Almanac to find the position of the sun at the start of survey
    almanac = Almanac(mjd_start=mjd_start)
    sun_moon_info = almanac.get_sun_moon_positions(mjd_start)
    sun_ra_start = sun_moon_info["sun_RA"].copy()

    footprints = make_rolling_footprints(
        fp_hp=footprints_hp,
        mjd_start=mjd_start,
        sun_ra_start=sun_ra_start,
        nslice=nslice,
        scale=rolling_scale,
        nside=nside,
        wfd_indx=wfd_indx,
        order_roll=1,
        n_cycles=3,
        uniform=rolling_uniform,
    )

    gaps_night_pattern = [True] + [False] * nights_off

    long_gaps = gen_long_gaps_survey(
        nside=nside,
        footprints=footprints,
        night_pattern=gaps_night_pattern,
        u_exptime=u_exptime,
        nexp=nexp,
    )

    # Set up the DDF surveys to dither
    u_detailer = detailers.FilterNexp(filtername="u", nexp=1, exptime=u_exptime)
    dither_detailer = detailers.DitherDetailer(per_night=per_night, max_dither=max_dither)
    details = [
        detailers.CameraRotDetailer(min_rot=-camera_ddf_rot_limit, max_rot=camera_ddf_rot_limit),
        dither_detailer,
        u_detailer,
        detailers.Rottep2RotspDesiredDetailer(),
    ]
    euclid_detailers = [
        detailers.CameraRotDetailer(min_rot=-camera_ddf_rot_limit, max_rot=camera_ddf_rot_limit),
        detailers.EuclidDitherDetailer(),
        u_detailer,
        detailers.Rottep2RotspDesiredDetailer(),
    ]
    ddfs = ddf_surveys(
        detailers=details,
        season_unobs_frac=ddf_season_frac,
        euclid_detailers=euclid_detailers,
        nside=nside,
        nexp=nexp,
    )

    greedy = gen_greedy_surveys(nside, nexp=nexp, footprints=footprints)
    neo = generate_twilight_near_sun(
        nside,
        night_pattern=ei_night_pattern,
        filters=ei_filters,
        n_repeat=ei_repeat,
        footprint_mask=footprint_mask,
        max_airmass=ei_am,
        max_elong=ei_elong_req,
        min_area=ei_area_req,
    )
    blobs = generate_blobs(
        nside,
        nexp=nexp,
        footprints=footprints,
        mjd_start=mjd_start,
        u_exptime=u_exptime,
    )
    twi_blobs = generate_twi_blobs(
        nside,
        nexp=nexp,
        footprints=footprints,
        wfd_footprint=wfd_footprint,
        repeat_night_weight=repeat_night_weight,
        night_pattern=reverse_ei_night_pattern,
    )

    roman_surveys = [
        gen_roman_on_season(nexp=nexp, exptime=29.2),
        gen_roman_off_season(nexp=nexp, exptime=29.2),
    ]
    if too:
        too_scale = 1.0
        sim_ToOs, event_table = gen_all_events(scale=too_scale, nside=nside)
        camera_rot_limits = [-80.0, 80.0]
        detailer_list = []
        detailer_list.append(
            detailers.CameraRotDetailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
        )
        # Let's make a footprint to follow up ToO events
        too_footprint = footprints_hp["r"] * 0 + np.nan
        too_footprint[np.where(footprints_hp["r"] > 0)[0]] = 1.0

        detailer_list.append(detailers.Rottep2RotspDesiredDetailer())
        toos = gen_too_surveys(
            nside=nside,
            detailer_list=detailer_list,
            too_footprint=too_footprint,
            split_long=split_long,
            n_snaps=nexp,
        )
        surveys = [toos, roman_surveys, ddfs, long_gaps, blobs, twi_blobs, neo, greedy]

    else:
        surveys = [roman_surveys, ddfs, long_gaps, blobs, twi_blobs, neo, greedy]

        sim_ToOs = None
        event_table = None
        fileroot = fileroot.replace("baseline", "no_too")

    scheduler = CoreScheduler(surveys, nside=nside)

    if args.setup_only:
        return scheduler
    else:
        years = np.round(survey_length / 365.25)
        observatory, scheduler, observations = run_sched(
            scheduler,
            survey_length=survey_length,
            verbose=verbose,
            filename=os.path.join(fileroot + "%iyrs.db" % years),
            extra_info=extra_info,
            nside=nside,
            illum_limit=illum_limit,
            mjd_start=mjd_start,
            event_table=event_table,
            sim_to_o=sim_ToOs,
        )
        return observatory, scheduler, observations


def sched_argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", dest="verbose", action="store_true", help="Print more output")
    parser.set_defaults(verbose=False)
    parser.add_argument("--survey_length", type=float, default=365.25 * 10, help="Survey length in days")
    parser.add_argument("--out_dir", type=str, default="", help="Output directory")
    parser.add_argument("--nexp", type=int, default=2, help="Number of exposures per visit")
    parser.add_argument("--dbroot", type=str, help="Database root")
    parser.add_argument(
        "--setup_only",
        dest="setup_only",
        default=False,
        action="store_true",
        help="Only construct scheduler, do not simulate",
    )
    parser.add_argument(
        "--nside",
        type=int,
        default=DEFAULT_NSIDE,
        help="Nside should be set to default (32) except for tests.",
    )
    parser.add_argument(
        "--mjd_plus",
        type=float,
        default=0,
        help="number of days to add to the mjd start",
    )
    parser.add_argument(
        "--split_long",
        dest="split_long",
        action="store_true",
        help="Split long ToO exposures into standard visit lengths",
    )
    parser.set_defaults(split_long=False)
    parser.add_argument("--no_too", dest="no_too", action="store_true")
    parser.set_defaults(no_too=False)

    return parser


if __name__ == "__main__":
    parser = sched_argparser()
    args = parser.parse_args()
    gen_scheduler(args)
