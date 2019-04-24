import numpy as np
import pickle
import pandas as pd
from collections import Counter, defaultdict
from .io import load_read_data, load_gt_data, load_ref, filter_ref
from .io import write_bin_table, write_pars_table, write_cont_table
from .io import write_snp_table, write_est_runs, write_sim_runs
from .utils import bins_from_bed, data2probs, init_pars, Pars, ParsHD
from .utils import guess_sex
from .fwd_bwd import fwd_bwd_algorithm, viterbi, update_transitions
from .genotype_emissions import update_post_geno, update_Ftau, update_snp_prob
from .genotype_emissions import update_emissions
from .read_emissions import update_contamination
from .rle import get_rle
from .decode import pred_sims
from .log import log_, setup_log
import itertools

COORDS = ["chrom", "map", "pos"]


def baum_welch(
    P,
    IX,
    pars,
    est_options,
    max_iter=1000,
    ll_tol=1e-1,
    gt_mode=False,
):
    O = est_options
    alpha0, alpha0_hap, trans, trans_hap, cont, error, F, tau, gamma_names, sex = pars
    gll_mode = not gt_mode
    ll = -np.inf
    n_states = len(alpha0)
    n_hap_states = len(alpha0_hap)
    n_gt = 3 if gt_mode else 3

    # create arrays for posterior, emissions
    Z = np.zeros((sum(IX.bin_sizes), n_states))  # P(Z | O)
    E = np.ones((sum(IX.bin_sizes), n_states))  # P(O | Z)
    # P(O, G | Z), scaled such that max for each row is 1
    # if gll_mode:
    SNP = np.zeros((IX.n_snps, n_states, n_gt))  # P(O, G | Z)
    PG = np.zeros((IX.n_snps, n_states, n_gt))  # P(G Z | O)
    # else:
    #    SNP = np.zeros((IX.n_snps, n_states))  # P(G | Z)

    gamma, emissions = [], []
    hap_gamma, hap_emissions = [], []
    row0 = 0
    for r, chrom in zip(IX.bin_sizes, IX.chroms):
        if chrom in IX.haplo_chroms:
            hap_gamma.append(Z[row0 : (row0 + r), :n_hap_states])
            hap_emissions.append(E[row0 : (row0 + r), :n_hap_states])
        else:
            gamma.append(Z[row0 : (row0 + r)])
            emissions.append(E[row0 : (row0 + r)])
        row0 += r

    s_scaling = update_snp_prob(
        SNP, P, IX, cont, error, F, tau, O['est_inbreeding'], gt_mode
    )

    e_scaling = update_emissions(E, SNP, P, IX)  # P(O | Z)
    scaling = e_scaling + s_scaling

    for it in range(max_iter):
        alpha, beta, n = fwd_bwd_algorithm(alpha0, emissions, trans, gamma)
        alpha_hap, beta_hap, n_hap = fwd_bwd_algorithm(
            alpha0_hap, hap_emissions, trans_hap, hap_gamma
        )

        ll, old_ll = (
            np.sum([np.sum(np.log(n_i)) for n_i in itertools.chain(n, n_hap)])
            + scaling,
            ll,
        )
        assert np.allclose(np.sum(Z, 1), 1)
        if np.isnan(ll):
            pass
        assert not np.isnan(ll)
        tpl = (
            IX.n_reads / 1000,
            IX.n_obs / 1000,
            IX.n_snps / 1000,
            IX.n_bins / 1000,
            it,
            np.mean(np.max(Z, 1) >= 0.95),
            ll,
            ll - old_ll,
        )
        log_.info("[%dk|%dk|%dk|%dk]: iter:%d |p95:%.3f\tLL:%.4f\tΔLL:%.4f" % tpl)
        if ll - old_ll < ll_tol:
            break

        if np.any(np.isnan(Z)):
            raise ValueError("nan observed in state posterior")
        if np.any(np.isnan(E)):
            raise ValueError("nan observed in emissions")

        # update stuff
        trans = update_transitions(
            trans, alpha, beta, gamma, emissions, n, est_inbreeding=O['est_inbreeding']
        )
        trans_hap = update_transitions(
            trans_hap,
            alpha_hap,
            beta_hap,
            hap_gamma,
            hap_emissions,
            n_hap,
            est_inbreeding=False
        )
        alpha0 = np.linalg.matrix_power(trans, 10000)[0]
        alpha0_hap = np.linalg.matrix_power(trans_hap, 10000)[0]

        if gamma_names is not None:
            log_.info("\t".join(gamma_names))
        log_.info("\t".join(["%.3f" % a for a in alpha0]))

        scaling = update_emission_stuff(
            it,
            E,
            P,
            PG,
            SNP,
            Z,
            IX,
            cont,
            error,
            F,
            tau,
            scaling,
            est_options,
            gt_mode,
        )

    update_post_geno(PG, SNP, Z, IX)
    pars = ParsHD(
        alpha0,
        alpha0_hap,
        trans,
        trans_hap,
        cont,
        error,
        F,
        tau,
        gamma_names,
        sex,
    )
    return (
        Z,
        PG,
        pars,
        ll,
        emissions,
        hap_emissions,
        (alpha, beta, n),
        (alpha_hap, beta_hap, n_hap),
    )


def update_emission_stuff(
    it,
    E,
    P,
    PG,
    SNP,
    Z,
    IX,
    cont,
    error,
    F,
    tau,
    scaling,
    est_options,
    gt_mode,
):
    O = est_options
    cond_cont = O['est_contamination'] and (it % O['freq_contamination'] == 0 or it < 3)

    cond_Ftau = O['est_F'] or O['est_tau'] and (it % O['freq_F'] == 0 or it < 3)

    if cond_Ftau or cond_cont:  # and gll_mode:
        update_post_geno(PG, SNP, Z, IX)

    if cond_cont and not gt_mode:
        delta = update_contamination(cont, error, P, PG, IX, est_options)
        if delta < 1e-5:  # when we converged, do not update contamination
            O['est_contamination'], cond_cont = False, False
            log_.info("stopping contamination updates")

    if cond_Ftau:
        delta = update_Ftau(F, tau, PG, P, IX, est_options)
        if delta < 1e-5:  # when we converged, do not update F
            O['est_F'], O['est_tau'] = False, False
            cond_Ftau, cond_F, cond_tau = False, False, False
            log_.info("stopping Ftau updates")
    if cond_Ftau or cond_cont or cond_tau:
        s_scaling = update_snp_prob(
            SNP,
            P,
            IX,
            cont,
            error,
            F,
            tau,
            est_inbreeding=O['est_inbreeding'],
            gt_mode=gt_mode,
        )  

        e_scaling = update_emissions(E, SNP, P, IX)  # P(O | Z)
        log_.info("e-scaling: %s", e_scaling)
        log_.info("s-scaling: %s", s_scaling)
        scaling = e_scaling + s_scaling

    return scaling


est_default = dict(
    [
        ("est_inbreeding", False),
        ("est_contamination", True),
        ("est_F", False),
        ("est_tau", False),
        ("est_error", False),
        ("freq_contamination", 1),
        ("freq_F", 1),
    ]
)


def run_admixfrog(
    infile,
    ref_files,
    states=("AFR", "VIN", "DEN"),
    cont_id="AFR",
    split_lib=True,
    bin_size=1e4,
    prior=None,
    ancestral=None,
    sex=None,
    pos_mode=False,
    autosomes_only=False,
    downsample=1,
    n_post_replicates=100,
    gt_mode=False,
    keep_loc=True,
    output=defaultdict(lambda: True),
    init=defaultdict(lambda: 1e-2),
    est=est_default,
    filter=defaultdict(lambda: None),
    **kwargs
):
    """admixture fragment inference
    this is typically run through the command-line interface. Type admixfrog --help for information
    on arguments
    """

    # numpy config
    np.set_printoptions(suppress=True, precision=4)
    np.seterr(divide="ignore", invalid="ignore")

    # by default, bin size is scaled by 10^6 - could be changed
    bin_size = bin_size if pos_mode else bin_size * 1e-6

    # loading data and reference
    if gt_mode:  # gt mode does not do read emissions, assumes genotypes are known
        data = load_read_data(infile)
    else:
        data = load_read_data(infile, split_lib, downsample)

    if (not est["est_contamination"] and init['c0'] == 0) or gt_mode:
        cont_id = None

    ref = load_ref(ref_files, states, cont_id, prior, ancestral, autosomes_only)
    ref = filter_ref(ref, states, **filter)
    ref = ref.drop_duplicates(COORDS)
    if pos_mode:
        ref.map = ref.pos

    # sexing stuff
    if sex is None:
        sex = guess_sex(data)

    log_.debug(ref.shape)
    data = data.merge(ref[COORDS], how="inner").dropna()
    log_.debug(data.shape)

    ref = ref.sort_values(COORDS)
    data = data.sort_values(COORDS)

    snp = data[COORDS].drop_duplicates()
    log_.debug(snp.shape)
    n_snps = snp.shape[0]
    snp["snp_id"] = range(n_snps)
    data = data.merge(snp)

    bins, IX = bins_from_bed(
        bed=ref.iloc[:, :5],
        snp=snp,
        data=data,
        bin_size=bin_size,
        pos_mode=pos_mode,
        sex=sex,
    )

    ref = ref.merge(snp[COORDS], "right")
    log_.debug(ref.shape)

    P = data2probs(data, ref, IX, states, cont_id, prior=prior, ancestral=ancestral)

    assert ref.shape[0] == P.alpha.shape[0] + P.alpha_hap.shape[0]
    del ref, snp

    pars = init_pars(states, sex, est_inbreeding=est["est_inbreeding"], **init)
    log_.info("done loading data")

    Z, G, pars, ll, emissions, hemissions, (_, beta, n), (_, bhap, nhap) = baum_welch(
        P, IX, pars, gt_mode=gt_mode, est_options=est, **kwargs
    )
    # pickle.dump((alpha, beta, n, emissions, pars), open("dump.pickle", "wb"))

    # output formating from here
    if output["output_pars"]:
        df_pars = write_pars_table(pars)

    if output["output_rsim"]:
        df_pred = pred_sims(
            trans=pars.trans,
            emissions=emissions,
            beta=beta,
            alpha0=pars.alpha0,
            n=n,
            n_homo=len(states),
            n_sims=n_post_replicates,
            est_inbreeding=est["est_inbreeding"],
            keep_loc=keep_loc,
        )
        df_pred["chrom"] = [IX.diplo_chroms[i] for i in df_pred.chrom.values]
        df_pred["state"] = [states[i] for i in df_pred.state.values]
        
        if len(bhap) > 0 :
            df_pred_hap = pred_sims(
                trans=pars.trans_hap,
                emissions=hemissions,
                beta=bhap,
                alpha0=pars.alpha0_hap,
                n=nhap,
                n_homo=len(states),
                n_sims=n_post_replicates,
                est_inbreeding=est["est_inbreeding"],
                keep_loc=keep_loc,
                decode=False
            )
            df_pred_hap["chrom"] = [IX.haplo_chroms[i] for i in df_pred_hap.chrom.values]
            df_pred_hap["state"] = [states[i] for i in df_pred_hap.state.values]
            df_pred = pd.concat((df_pred, df_pred_hap))

        write_sim_runs(df_pred)

    if output["output_bin"] or output["output_rle"]:
        viterbi_path = viterbi(pars.alpha0, pars.trans, emissions)
        viterbi_path_hap = viterbi(pars.alpha0_hap, pars.trans_hap, hemissions)
        V = np.array(pars.gamma_names)[np.hstack(viterbi_path)]
        viterbi_df = pd.Series(V, name="viterbi")

        df_bin = write_bin_table(Z, bins, viterbi_df, pars.gamma_names, IX)

    if output["output_snp"]:
        df_snp = write_snp_table(data=data, G=G, Z=Z, IX=IX, gt_mode=gt_mode)

    if output["output_cont"]:
        df_cont = write_cont_table(data, pars.cont, pars.error)

    if output["output_rle"]:
        df_rle = get_rle(df_bin, states, init["run_penalty"])
        write_est_runs(df_rle)

    return df_bin, df_snp, df_cont, df_pars, df_rle, df_pred
