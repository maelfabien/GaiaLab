"""
file agis.py
Contains implementation of classes Calc_source and Agis

author: Luca Zampieri

*Notes:*
    - In this file, when there is a reference, unless explicitly stated otherwise,
    it refers to Lindegren main article:
    "The astronometric core solution for the Gaia mission - overview of models,
    algorithms, and software implementation" by L. Lindegren, U. Lammer,
    D. Hobbs, W. O'Mullane, U. Bastian, and J.Hernandez
    The reference is usually made in the following way: Ref. Paper eq. [1]

    - t (float): time from J2000 [days]
    such that t_ep = 0
"""
# # Imports
# Local modules
import frame_transformations as ft
import constants as const
from satellite import Satellite
from source import Source
from agis_functions import *

# global modules
import numpy as np
from scipy.interpolate import BSpline
from scipy import sparse as sps


class Calc_source:
    """
    Contains the calculated parameters per source
    """
    def __init__(self, name=None, obs_times=[], source_params=None, mu_radial=None, mean_color=0,
                 source=None):
        """
        Initial guess of the parameters
        :source_params: alpha, delta, parallax, mu_alpha, mu_delta
        """
        if source is not None:
            name = 'Calc_' + source.name
            params = source.get_parameters()
            source_params = params[0:-1]
            mu_radial = params[-1]
            mean_color = source.mean_color
        self.name = name
        self.obs_times = obs_times  # times at which it has been observed
        self.s_params = source_params  # position at which it has been observed
        self.mu_radial = mu_radial  # not considered an unknown of the problem
        self.s_old = [self.s_params]
        self.errors = []
        self.mean_color = mean_color

    def set_params(self, params):
        self.s_params = params
        self.s_old = [self.s_params]


class Agis:

    def __init__(self, sat, calc_sources=[], real_sources=[], attitude_splines=None,
                 verbose=False, spline_degree=3, attitude_regularisation_factor=0,
                 updating='attitude', degree_error=0, double_telescope=False):
        """
        Also contains:
        **Temporary variables**
        self.astro_param : the astronometric parameters for the source we're examining
        self.obs_times : the observation times for a given source
        **Variables**
        # The four parameter vector
        # self.s_param  # source parameters (for each calc_source)
        # self.att_coeffs  # attitude parameters
        """
        # Objects:
        self.calc_sources = calc_sources
        self.real_sources = real_sources
        self.sat = sat

        # Constants
        self.k = spline_degree  # degree of the interpolating polynomial
        self.M = self.k + 1
        self.attitude_regularisation_factor = attitude_regularisation_factor
        self.verbose = verbose
        self.updating = updating
        self.consider_stellar_aberation = False
        self.degree_error = degree_error  # [only for source] deviation in vertical direction of the attitude
        self.double_telescope = double_telescope  # bool indicating if we use the double_telescope config

        # Mutable:
        self.iter_counter = 0
        self.N = 0  # not necessary

        # Setting observation times
        all_obs_times = []
        self.time_dict = {}
        for source_index, calc_source in enumerate(self.calc_sources):
            all_obs_times += list(calc_source.obs_times)
            for t in calc_source.obs_times:
                self.time_dict[t] = source_index
        self.all_obs_times = np.sort(all_obs_times)

        # Set attitude
        if attitude_splines is not None:  # Set everything for the attitude
            c, t, s = extract_coeffs_knots_from_splines(attitude_splines, self.k)
            self.c = c.copy()
            self.att_coeffs, self.att_knots, self.attitude_splines = (c, t, s)
            self.att_bases = get_basis_Bsplines(self.att_knots, self.att_coeffs[0], self.k, self.all_obs_times)
            self.N = self.att_coeffs.shape[1]  # number of coeffs per parameter

    # ### Generic functions for all kind of updating -----------------------------------
    def reset_iterations(self):
        print('Not resetting everything! Call again the solver instead')
        self.iter_counter = 0
        for calc_source in self.calc_sources:
            calc_source.s_old = []
            calc_source.errors = []

    def error_function(self):
        """
        Ref. Paper eq. [24]
        Compute the error function Q
        """
        error = 0
        for source_index, s in enumerate(self.calc_sources):
            if self.verbose:
                print('source: {}'.format(s.s_params))
            for j, t_L in enumerate(s.obs_times):
                R_L = self.compute_R_L(source_index, t_L)
                error += R_L ** 2
        return error  # / self.all_obs_times.shape[0])/const.rad_per_mas

    def get_field_angles(self, source_index, t):
        """ :returns: [eta_obs, zeta_obs, eta_calc, zeta_calc]"""
        # Set attitude, it depends if we wanna update only sources or also attitude params
        if self.updating == 'source':
            attitude = self.get_attitude_for_source(source_index, t)
            attitude_gaia = attitude
        elif self.updating == 'scanned source':
            attitude = self.sat.func_attitude(t)
            attitude_gaia = attitude
        elif self.updating == 'attitude':
            attitude = self.get_attitude(t)
            attitude_gaia = self.sat.func_attitude(t)
        else:
            raise ValueError('incorrect value for self.updating')

        eta_obs, zeta_obs = observed_field_angles(self.real_sources[source_index],
                                                  attitude_gaia,
                                                  self.sat, t, self.double_telescope)
        eta_calc, zeta_calc = calculated_field_angles(self.calc_sources[source_index],
                                                      attitude,
                                                      self.sat, t, self.double_telescope)
        return eta_obs, zeta_obs, eta_calc, zeta_calc

    def deviate_field_angles_color_aberration(self, source_index, t, angles):
        """ apply color aberration deviation to field angles (eta, zeta)"""
        # # WARNING: check also deviation in the source update
        f_color = self.real_sources[source_index].func_color(t)  # # TODO: separate eta zeta
        m_color = self.real_sources[source_index].mean_color
        eta_obs, zeta_obs, eta_calc, zeta_calc = angles
        # eta_obs, zeta_obs = compute_deviated_angles_color_aberration(eta_obs, zeta_obs, f_color, self.degree_error)
        # eta_calc, zeta_calc = compute_deviated_angles_color_aberration(eta_calc, zeta_calc, m_color, self.degree_error)
        return eta_obs, zeta_obs, eta_calc, zeta_calc

    def compute_R_L(self, source_index, t):
        """ Ref. Paper eq. [25]-[26]
        R = eta_obs + zeta_obs - eta_calc - zeta_calc """
        # WARNING: maybe source is not in the field of vision of sat at time t!
        R_eta, R_zeta = (0, 0)

        angles = self.get_field_angles(source_index, t)
        eta_obs, zeta_obs, eta_calc, zeta_calc = self.deviate_field_angles_color_aberration(source_index, t, angles)

        R_eta = eta_obs - eta_calc  # AL
        R_zeta = zeta_obs - zeta_calc  # AC
        R_L = R_eta + R_zeta
        if self.verbose:
            print(t, 'observed: ', eta_obs, zeta_obs)
            print(t, 'computed: ', eta_calc, zeta_calc)
        return R_L

    def iterate(self, num):
        """
        Do _num_ iterations
        """
        for i in range(num):
            self.iter_counter += 1
            print('***** Iteration: {} *****'.format(self.iter_counter))
            if self.verbose:
                print('Error before iteration: {}'.format(self.error_function()))

            if self.updating == 'source' or self.updating == 'scanned source':
                self.update_S_block()

            elif self.updating == 'attitude':
                self.update_A_block()
                error = error_between_func_attitudes(self.all_obs_times, self.sat.func_attitude, self.get_attitude)
                print('attitude error:', error)

            print('Error after iteration: {}'.format(self.error_function()))
    # ### End generic functions ################################################

    #
    # ### Functions only for sources updating (source and scanned source)
    def update_S_block(self):
        """ Performs the update of the source parameters """
        for i, calc_source in enumerate(self.calc_sources):
            calc_source.s_old.append(calc_source.s_params.copy())
            calc_source.errors.append(self.error_function())
            self.update_block_S_i(i)

    def update_block_S_i(self, source_index):
        """ Ref. Paper eq. [57]
        update source #i"""
        calc_source = self.calc_sources[source_index]
        A = self.block_S_error_rate_matrix(source_index)
        W = np.eye(len(calc_source.obs_times))
        h = self.compute_h(source_index)
        LHS = A.transpose() @ W @ A
        RHS = A.transpose() @ W @ h
        d = np.linalg.solve(LHS, RHS)
        d = d.flatten()
        self.calc_sources[source_index].s_params[:] += d
        if self.verbose:
            print('dim A:{}\ndim W:{}\ndim h:{}\ndim d:{}'
                  .format(A.shape, W.shape, h.shape, d.shape))

    def du_tilde_ds(self, source_index):
        """ Ref. Paper eq. [73]
        Compute dũ_ds far a given source
        :param:
        :returns:
        :used names:
            - b_G(t) barycentric position of Gaia at the time of observation, also
              called barycentric ephemeris of the Gaia Satellite
            - t_B barycentric time (takes into account the Römer delay)
        Notes: t_ep in the paper is not used since we assume t_ep=0 and start counting the time from J2000
        """
        # In this function consider all u as being ũ! (for notation we call them here u)
        # Values needed to compute the derivatives
        calc_source = self.calc_sources[source_index]
        n_i = len(calc_source.obs_times)  # the number of observations
        du_ds = np.zeros((5, 3, n_i))
        alpha = calc_source.s_params[0]
        delta = calc_source.s_params[1]
        p, q, r = ft.compute_pqr(alpha, delta)
        r.shape = (3, 1)  # reshapes r
        # For each observation compute du/ds
        for j, t_l in enumerate(calc_source.obs_times):  # t_l being the observation time
            b_G = self.sat.ephemeris_bcrs(t_l)
            t_B = t_l  # + np.dot(r, b_G) / const.c
            tau = t_B - const.t_ep
            # Compute derivatives
            du_dalpha = p
            du_ddelta = q
            du_dparallax = compute_du_dparallax(r, b_G)
            du_dmualpha = p*tau
            du_dmudelta = q*tau
            du_ds[:, :, j] = [du_dalpha, du_ddelta, du_dparallax, du_dmualpha, du_dmudelta]
        if self.verbose:
            print('du_ds.shape: {}'.format(du_ds.shape))
        return du_ds

    def du_ds(self, source_index):
        """ [not yet complete] Ref. Paper eq. [75]
        returns the derivative of the proper direction w.r.t. the astronomic
        parameters.
        take into account aberrationn of light
        :param du_ds_tilde: in the CoRMS frame (lmn)
        :returns du_ds: in the SRS frame (xyz)
        """
        # TODO: implement stellar aberation
        if self.consider_stellar_aberation:
            raise ValueError('Stellar aberation not yet implemented')
        # u = compute_coordinate_direction()
        # coeff = (1-u.transpose()*v_g/const.c)I - u*v_G.transpose()/const.c
        calc_source = self.calc_sources[source_index]
        coeff = 1
        C_du_ds = coeff * self.du_tilde_ds(source_index)
        S_du_ds = self.C_du_ds_to_S_du_ds(source_index, C_du_ds)
        if self.verbose:
            print('S_du_ds shape: {}'.format(S_du_ds.shape))
        return S_du_ds

    def C_du_ds_to_S_du_ds(self, source_index, C_du_ds):
        """ Ref. Paper eq. [72]
        rotate the frame from CoRMS (lmn) to SRS (xyz) for du_ds
        """
        calc_source = self.calc_sources[source_index]
        S_du_ds = np.zeros(C_du_ds.shape)
        for i in range(C_du_ds.shape[0]):  # TODO: remove these ugly for loop
            for j in range(C_du_ds.shape[-1]):
                t_L = calc_source.obs_times[j]
                if self.updating == 'source':
                    attitude = self.get_attitude_for_source(source_index, t_L)
                elif self.updating == 'scanned source':
                    attitude = self.sat.func_attitude(t_L)
                else:  # attitude = self.get_attitude(t_L)
                    raise ValueError('Not yet implemented for this case')
                S_du_ds[i, :, j] = ft.lmn_to_xyz(attitude, C_du_ds[i, :, j])
        return S_du_ds

    def dR_ds(self, source_index):
        """ Ref. Paper eq. [71]
        Computes the derivative of the error (R_l) wrt the 5 astronomic parameters
        s_i transposed.
        :param kind: either AL for ALong scan direction or AC for ACross scan direction
        :returns:
        """

        calc_source = self.calc_sources[source_index]
        du_ds = self.du_ds(source_index)
        dR_ds_AL = np.zeros((len(calc_source.obs_times), 5))
        dR_ds_AC = np.zeros(dR_ds_AL.shape)

        for i, t_L in enumerate(calc_source.obs_times):
            if self.updating == 'source':
                attitude = self.get_attitude_for_source(source_index, t_L)
            elif self.updating == 'scanned source':
                attitude = self.sat.func_attitude(t_L)
            else:
                raise ValueError('not yet implemented for this kind of updating')
            # Set double_telescope to False to get phi
            phi, zeta = calculated_field_angles(calc_source, attitude, self.sat, i, double_telescope=False)
            phi, zeta = compute_deviated_angles_color_aberration(phi, zeta, calc_source.mean_color, self.degree_error)
            m, n, u = compute_mnu(phi, zeta)
            dR_ds_AL[i, :] = -m @ du_ds[:, :, i].transpose() * helpers.sec(zeta)
            dR_ds_AC[i, :] = -n @ du_ds[:, :, i].transpose()

        return dR_ds_AL + dR_ds_AC

    def block_S_error_rate_matrix(self, source_index):
        """ Ref. Paper eq. [58]
        error matrix for the block update S"""
        return -self.dR_ds(source_index)

    def compute_h(self, source_index):
        """Ref. Paper eq. [59]
        Source update Right hand side"""
        calc_source = self.calc_sources[source_index]
        h = np.zeros((len(calc_source.obs_times), 1))
        for i, t_L in enumerate(calc_source.obs_times):
            h[i, 0] = self.compute_R_L(source_index, t_L)
        if self.verbose:
            print('h: {}'.format(h))
        return h

    def get_attitude_for_source(self, source_index, t):
        """ For only source updating with color aberration.
        Change if condition to decide which sources are affected by that aberration"""
        if source_index < 0:
            deviation = self.degree_error * const.rad_per_deg  # number in degrees and converted in radians
        else:
            deviation = 0
        return attitude_from_alpha_delta(self.real_sources[source_index], self.sat, t, deviation)
    # ### End function only for source updating ################################

    #
    # ### For attitude update --------------------------------------------------
    def get_attitude(self, t, unit=True):
        s_w = self.attitude_splines[0]
        s_x = self.attitude_splines[1]
        s_y = self.attitude_splines[2]
        s_z = self.attitude_splines[3]
        attitude = Quaternion(s_w(t), s_x(t), s_y(t), s_z(t))
        if unit:
            attitude = attitude.unit()
        return attitude

    def actualise_splines(self):
        for i in range(self.attitude_splines.shape[0]):
            self.attitude_splines[i] = BSpline(self.att_knots, self.att_coeffs[i], k=self.k)

    def normalize_coefficients(self):
        for i in range(self.N):
            self.att_coeffs[:, i] /= np.linalg.norm(self.att_coeffs[:, i])

    def update_A_block(self):  # one
        """ solve the components together"""
        LHS = self.compute_attitude_LHS()
        # LHS = self.N_aa
        RHS = self.compute_attitude_RHS()
        # RHS = self.h
        d = np.linalg.solve(LHS, RHS)

        # L = np.linalg.cholesky(LHS)
        # y = np.linalg.solve(L, RHS)
        # d = np.linalg.solve(L.T, y)
        # d = np.linalg.lstsq(LHS, RHS)  # not what it is for
        self.d = d
        c_update = d.reshape(self.att_coeffs.shape)
        for i in range(0, self.att_coeffs.shape[1]):
            c_update[0, i] = d[i*4]
            c_update[1, i] = d[i*4+1]
            c_update[2, i] = d[i*4+2]
            c_update[3, i] = d[i*4+3]
        self.c_update = c_update.copy()
        self.att_coeffs[:, :] += c_update[:, :].copy()
        self.actualise_splines()  # Create the new splines

    def update_A_block_bis(self):  # bis
        """ updates the four components separately (wrong but not by much)"""
        LHS = self.compute_attitude_LHS()
        RHS = self.compute_attitude_RHS()
        for i in range(4):
            d = np.linalg.solve(LHS[i::4, i::4], RHS[i::4])
            c_update = d.reshape(-1)
            self.att_coeffs[i] += c_update
        self.actualise_splines()  # Create the new splines

    def compute_attitude_LHS(self):
        N_aa_dim = self.att_coeffs.shape[1]  # *4
        print('N_aa_dim:', N_aa_dim)
        N_aa = np.zeros((N_aa_dim*4, N_aa_dim*4))
        for n in range(0, N_aa_dim):  # # TODO:  take advantage of the symmetry
            for m in range(0, N_aa_dim):  # # TODO: avoid doing the brute force version
                # for m in range(max((n-self.k), 0), min((n+self.k)+1, N_aa_dim+1)):
                N_aa[n*4:n*4+4, m*4:m*4+4] = self.compute_Naa_mn(m, n)
        self.N_aa = N_aa
        return N_aa

    def compute_attitude_RHS(self):
        N_aa_dim = self.att_coeffs.shape[1]
        RHS = np.zeros((N_aa_dim*4, 1))
        for n in range(0, N_aa_dim):
            RHS[n*4:n*4+4] = self.compute_attitude_RHS_n(n)
        self.h = RHS.copy()
        return RHS

    def attitude_LHS_band(self):
        N_aa_band = np.zeros((self.N*4, 16))
        for n in range(0, self.N):
            for i, m in enumerate(range(n, min(n+4, self.N*4))):
                N_aa_band[n*4:n*4+4, i:i+4] = self.compute_Naa_mn(m, n)
        return N_aa_band

    def attitude_LHS_from_band(N_aa_band):
        N_aa_sps = helpers.get_sparse_diagonal_matrix_from_half_band(N_aa_band)
        return N_aa_sps.toarray()

    def get_source_index(self, t):
        """ get the index of the source corresponding to observation t"""
        if t in self.time_dict:
            return self.time_dict[t]
        else:
            raise ValueError('time not in time_dict')

    def compute_attitude_RHS_n(self, n_index):
        rhs = np.zeros((4, 1))
        time_support_spline_n = get_times_in_knot_interval(self.all_obs_times, self.att_knots, n_index, self.M)
        for i, t_L in enumerate(time_support_spline_n):
            source_index = self.get_source_index(t_L)
            calc_source = self.calc_sources[source_index]
            attitude = self.get_attitude(t_L, unit=False)
            left_index = get_left_index(self.att_knots, t_L, M=self.M)
            obs_time_index = list(self.all_obs_times).index(t_L)

            # Compute the regulation part
            coeff_basis_sum = compute_coeff_basis_sum(self.att_coeffs, self.att_bases,
                                                      left_index, self.M, obs_time_index)
            D_L = compute_attitude_deviation(coeff_basis_sum)
            dDL_da_n = compute_DL_da_i(coeff_basis_sum, self.att_bases, obs_time_index, n_index)
            # dDL_da_n = compute_DL_da_i_from_attitude(attitude, self.att_bases, obs_time_index, n_index)
            regularisation_part = self.attitude_regularisation_factor**2 * dDL_da_n * D_L
            # # WARNING: Here we put the Across scan and the along scan together
            dR_dq = compute_dR_dq(calc_source, self.sat, attitude, t_L)
            dR_da_n = dR_da_i(dR_dq, self.att_bases[n_index, obs_time_index])
            R_L = self.compute_R_L(source_index, t_L)

            rhs += dR_da_n * R_L + regularisation_part
        return -rhs

    def compute_Naa_mn(self, m_index, n_index):
        """compute dR/da (i.e. wrt coeffs)"""
        Naa_mn = np.zeros((4, 4))
        time_support_spline_m = get_times_in_knot_interval(self.all_obs_times, self.att_knots, m_index, self.M)
        time_support_spline_n = get_times_in_knot_interval(self.all_obs_times, self.att_knots, n_index, self.M)
        time_support_spline_mn = np.sort(helpers.get_lists_intersection(time_support_spline_m, time_support_spline_n))

        for i, t_L in enumerate(time_support_spline_mn):
            # for i, t_L in enumerate(self.all_obs_times):
            calc_source = self.calc_sources[self.get_source_index(t_L)]
            attitude = self.get_attitude(t_L, unit=False)
            left_index = get_left_index(self.att_knots, t=t_L, M=self.M)
            obs_time_index = list(self.all_obs_times).index(t_L)

            # Compute the regulation part
            coeff_basis_sum = compute_coeff_basis_sum(self.att_coeffs, self.att_bases,
                                                      left_index, self.M, obs_time_index)
            dDL_da_n = compute_DL_da_i(coeff_basis_sum, self.att_bases, obs_time_index, n_index)
            dDL_da_m = compute_DL_da_i(coeff_basis_sum, self.att_bases, obs_time_index, m_index)
            # dDL_da_n = compute_DL_da_i_from_attitude(attitude, self.att_bases, obs_time_index, n_index)
            # dDL_da_m = compute_DL_da_i_from_attitude(attitude, self.att_bases, obs_time_index, m_index)
            regularisation_part = self.attitude_regularisation_factor**2 * dDL_da_n @ dDL_da_m.T

            # Compute the original objective function part
            # # WARNING: Here we put the Across scan and the along scan together
            dR_dq = compute_dR_dq(calc_source, self.sat, attitude, t_L)
            dR_da_m = dR_da_i(dR_dq, self.att_bases[m_index, obs_time_index])
            dR_da_n = dR_da_i(dR_dq, self.att_bases[n_index, obs_time_index])
            Naa_mn += regularisation_part + dR_da_n @ dR_da_m.T

            if self.verbose:
                if m_index >= 0:
                    if i >= 0:
                        print('**** m:', m_index, '**** n:', n_index, '**** i:', i)
                        print('dR_dq: ', dR_dq)
                        print('dR_da_m', dR_da_m)
                        print('dR_da_n', dR_da_n)
                        print('Naa_mn', Naa_mn)
        return Naa_mn  # np.eye(4)


if __name__ == '__main__':
    print('Executing agis.py as main file')
