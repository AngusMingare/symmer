import warnings
import itertools
import numpy as np
import networkx as nx
import multiprocessing as mp
import qubovert as qv
from cached_property import cached_property
from time import time
from functools import reduce
from typing import Optional, Union, Tuple, List
from matplotlib import pyplot as plt
from scipy.optimize import differential_evolution, shgo
from symmer.operators import PauliwordOp, IndependentOp, AntiCommutingOp, QuantumState
from symmer.operators.utils import check_adjmat_noncontextual, binomial_coefficient

class NoncontextualOp(PauliwordOp):
    """ Class for representing noncontextual Hamiltonians

    Noncontextual Hamiltonians are precisely those whose terms may be reconstructed 
    under the Jordan product (AB = {A, B}/2) from a generating set of the form 
    G ∪ {C_1, ..., C_M} where {C_i, C_j}=0 for i != j and G commutes universally.
    Refer to https://arxiv.org/abs/1904.02260 for further details. 
    
    """
    up_method = 'seq_rot'

    def __init__(self,
            symp_matrix,
            coeff_vec
        ):
        """
        """
        super().__init__(symp_matrix, coeff_vec)
        assert(self.is_noncontextual), 'Specified operator is contextual.'
        # extract the symmetry generating set G and clique operator C(r)
        self.noncontextual_generators()
        # Reconstruct the noncontextual Hamiltonian into its G and C(r) components
        self.noncontextual_reconstruction()
        
    @classmethod
    def from_PauliwordOp(cls, H) -> "NoncontextualOp":
        """ for convenience, initialize from an existing PauliwordOp
        """
        noncontextual_operator = cls(
            H.symp_matrix,
            H.coeff_vec
        )
        return noncontextual_operator

    @classmethod
    def from_hamiltonian(cls, 
            H: PauliwordOp, 
            strategy: str = 'diag', 
            basis: PauliwordOp = None, 
            DFS_runtime: int = 10,
            override_noncontextuality_check: bool = True
        ) -> "NoncontextualOp":
        """ Given a PauliwordOp, extract from it a noncontextual sub-Hamiltonian by the specified strategy
        """
        if not override_noncontextuality_check:
            if H.is_noncontextual:
                warnings.warn('input H is already noncontextual ignoring strategy')
                return cls.from_PauliwordOp(H)
        
        if strategy == 'diag':
            return cls._diag_noncontextual_op(H)
        elif strategy == 'basis':
            return cls._from_basis_noncontextual_op(H, basis)
        elif strategy.find('DFS') != -1:
            _, strategy = strategy.split('_')
            return cls._dfs_noncontextual_op(H, strategy=strategy, runtime=DFS_runtime)
        elif strategy.find('SingleSweep') != -1:
            _, strategy = strategy.split('_')
            return cls._single_sweep_noncontextual_operator(H, strategy=strategy)
        else:
            raise ValueError(f'Unrecognised noncontextual operator strategy {strategy}')

    @classmethod
    def _diag_noncontextual_op(cls, H: PauliwordOp) -> "NoncontextualOp":
        """ Return the diagonal terms of the PauliwordOp - this is the simplest noncontextual operator
        """
        mask_diag = np.where(~np.any(H.X_block, axis=1))
        noncontextual_operator = cls(
            H.symp_matrix[mask_diag],
            H.coeff_vec[mask_diag]
        )
        return noncontextual_operator

    @classmethod
    def _dfs_noncontextual_op(cls, H: PauliwordOp, runtime=10, strategy='magnitude') -> "NoncontextualOp":
        """ function orders operator by coeff mag
        then going from first term adds ops to a pauliword op ensuring it is noncontextual
        adds to a tracking list and then changes the original ordering so first term is now at the end
        repeats from the start (aka generating a list of possible noncon Hamiltonians)
        from this list one can then choose the noncon op with the most terms OR largest sum of abs coeff weights
        cutoff time ensures if the number of possibilities is large the function will STOP and not take too long

        """
        operator = H.sort(by='magnitude')
        noncontextual_ops = []

        n=0
        start_time = time()
        while n < H.n_terms and time()-start_time < runtime:
            order = np.roll(np.arange(H.n_terms), -n)
            ordered_operator = PauliwordOp(
                symp_matrix=operator.symp_matrix[order],
                coeff_vec=operator.coeff_vec[order]
            )
            noncontextual_operator = PauliwordOp.empty(H.n_qubits)
            for op in ordered_operator:
                noncon_check = noncontextual_operator + op
                if noncon_check.is_noncontextual:
                    noncontextual_operator += op
            noncontextual_ops.append(noncontextual_operator)
            n+=1

        if strategy == 'magnitude':
            noncontextual_operator = sorted(noncontextual_ops, key=lambda x:-np.sum(abs(x.coeff_vec)))[0]
        elif strategy == 'largest':
            noncontextual_operator = sorted(noncontextual_ops, key=lambda x:-x.n_terms)[0]
        else:
            raise ValueError('Unrecognised noncontextual operator strategy.')

        return cls.from_PauliwordOp(noncontextual_operator)

    @classmethod
    def _diag_first_noncontextual_op(cls, H: PauliwordOp) -> "NoncontextualOp":
        """ Start from the diagonal noncontextual form and append additional off-diagonal
        contributions with respect to their coefficient magnitude.
        """
        noncontextual_operator = cls._diag_noncontextual_op(H)
        # order the remaining terms by coefficient magnitude
        off_diag_terms = (H - noncontextual_operator).sort(by='magnitude')
        # append terms that do not make the noncontextual_operator contextual!
        for term in off_diag_terms:
            if (noncontextual_operator+term).is_noncontextual:
                noncontextual_operator+=term
        
        return cls.from_PauliwordOp(noncontextual_operator)

    @classmethod
    def _single_sweep_noncontextual_operator(cls, H, strategy='magnitude') -> "NoncontextualOp":
        """ Order the operator by some sorting key (magnitude, random or CurrentOrder)
        and then sweep accross the terms, appending to a growing noncontextual operator
        whenever possible.
        """
        if strategy=='magnitude':
            operator = H.sort(by='magnitude')
        elif strategy=='random':
            order = np.arange(H.n_terms)
            np.random.shuffle(order)
            operator = PauliwordOp(
                H.symp_matrix[order],
                H.coeff_vec[order]
            )
        elif strategy =='CurrentOrder':
            operator = H
        else:
            raise ValueError('Unrecognised strategy, must be one of magnitude, random or CurrentOrder')            

        # initialize noncontextual operator with first element of input operator
        noncon_indices = np.array([0])
        adjmat = np.array([[True]], dtype=bool)
        for index, term in enumerate(operator[1:]):
            # pad the adjacency matrix term-by-term - avoids full construction each time
            adjmat_vector = np.append(term.commutes_termwise(operator[noncon_indices]), True)
            adjmat_padded = np.pad(adjmat, pad_width=((0, 1), (0, 1)), mode='constant')
            adjmat_padded[-1,:] = adjmat_vector; adjmat_padded[:,-1] = adjmat_vector
            # check whether the adjacency matrix has a noncontextual structure
            if check_adjmat_noncontextual(adjmat_padded):
                noncon_indices = np.append(noncon_indices, index+1)
                adjmat = adjmat_padded

        return cls.from_PauliwordOp(operator[noncon_indices])

    @classmethod
    def _from_basis_noncontextual_op(cls, H: PauliwordOp, generators: PauliwordOp) -> "NoncontextualOp":
        """ Construct a noncontextual operator given a noncontextual basis, via the Jordan product ( regular matrix product if the operators commute, and equal to zero if the operators anticommute.)
        """
        assert generators is not None, 'Must specify a noncontextual basis.'
        #assert generators.is_noncontextual, 'Basis is contextual.'

        _, noncontextual_terms_mask = H.jordan_generator_reconstruction(generators)
        return cls.from_PauliwordOp(H[noncontextual_terms_mask])
    
    def draw_graph_structure(self, 
            clique_lw=1,
            symmetry_lw=.25,
            node_colour='black',
            node_size=20,
            seed=None,
            axis=None
        ):
        """ Draw the noncontextual graph structure
        """
        adjmat = self.adjacency_matrix.copy()
        adjmat = self.adjacency_matrix.copy()
        index_symmetries = np.where(np.all(adjmat, axis=1))[0]
        np.fill_diagonal(adjmat, False)
        
        G = nx.Graph()
        for i,j in list(zip(*np.where(adjmat))):
            if i in index_symmetries or j in index_symmetries:
                G.add_edge(i,j,color='grey',weight=symmetry_lw)
            else:
                G.add_edge(i,j,color='black',weight=clique_lw)

        pos = nx.spring_layout(G, seed=seed)
        edges = G.edges()
        colors = [G[u][v]['color'] for u,v in edges]
        weights = [G[u][v]['weight'] for u,v in edges]
        nx.draw(G, pos, edge_color=colors, width=weights, 
                node_color=node_colour, node_size=node_size, ax=axis)

    def noncontextual_generators(self) -> None:
        """ Find an independent generating set for the noncontextual operator
        """
        # identify the symmetry generating set
        self.symmetry_generators = IndependentOp.symmetry_generators(self)#, commuting_override=True)
        # mask the symmetry terms within the noncontextual operator
        _, symmetry_mask = self.generator_reconstruction(self.symmetry_generators)
        # identify the reamining commuting cliques
        self.decomposed = self[~symmetry_mask].clique_cover(edge_relation='C')
        self.n_cliques = len(self.decomposed)
        if self.n_cliques > 0:
            # choose clique representatives with the greatest coefficient
            self.clique_operator = AntiCommutingOp.from_PauliwordOp(
                sum([C.sort()[0] for C in self.decomposed.values()])
            )
        else:
            self.clique_operator = PauliwordOp.empty(self.n_qubits).cleanup()
        # extract the universally commuting noncontextual terms (i.e. those which may be constructed from symmetry generators)
        self.decomposed['symmetry'] = self[symmetry_mask]
        
    def noncontextual_reconstruction(self) -> None:
        """ Reconstruct the noncontextual operator in each independent basis GuCi - one for every clique.
        This mitigates against dependency between the symmetry generators G and the clique representatives Ci
        """
        noncon_generators = PauliwordOp(
            np.vstack([self.symmetry_generators.symp_matrix, self.clique_operator.symp_matrix]),
            np.ones(self.symmetry_generators.n_terms + self.n_cliques)
        )
        # Cannot simultaneously know eigenvalues of cliques so we peform a generator reconstruction
        # that respects the jordan product A*B = {A, B}/2, i.e. anticommuting elements are zeroed out
        jordan_recon_matrix, successful = self.jordan_generator_reconstruction(noncon_generators)
        assert(np.all(successful)), 'The generating set is not sufficient to reconstruct the noncontextual Hamiltonian'
        self.G_indices = jordan_recon_matrix[:, :self.symmetry_generators.n_terms]
        self.C_indices = jordan_recon_matrix[:, self.symmetry_generators.n_terms:]
        self.mask_S0 = ~np.any(self.C_indices, axis=1)
        self.mask_Ci = self.C_indices.astype(bool).T
        # individual elements of r_part commute with all of G_part - taking products over G_part with
        # a single element of r_part will therefore never produce a complex phase, but might result in
        # a sign flip that must be accounted for in the generator reconstruction:
        multiply_indices = lambda inds:reduce(
            lambda x,y:x*y, # pairwise multiplication of Pauli factors
            noncon_generators[inds], # index the relevant noncontextual generating elements
            PauliwordOp.from_list(['I'*self.n_qubits]) # initialise product with identity
        ).coeff_vec[0].real

        self.pauli_mult_signs = np.array(
            list(map(multiply_indices,jordan_recon_matrix.astype(bool)))
        ).astype(int)

    @ cached_property
    def symmetrized_operator(self, expansion_order=1):
        """ Get the symmetrized noncontextual operator S_0 - sqrt(S_1^2 + .. S_M^2).
        In the infinite limit of expansion_order the ground state of this operator
        will coincide exactly with the true noncontextual operator. This is used
        for xUSO solver since this reformulation of the Hamiltonian is polynomial.
        """
        Si_list = [self.decomposed['symmetry']]
        for i in range(self.n_cliques):
            Ci = self.decomposed[i][0]; Ci.coeff_vec[0]=1
            Si = Ci*self.decomposed[i]
            Si_list.append(Si)

        S = sum([Si**2 for Si in Si_list[1:]])
        norm = np.linalg.norm(S.coeff_vec, ord=2)
        S *= (1/norm)
        I = PauliwordOp.from_list(['I'*self.n_qubits])
        terms = [
            (I-S)**n * (-1)**n * binomial_coefficient(.5, n) 
            for n in range(expansion_order+1)
        ] # power series expansion of the oeprator root
        S_root = sum(terms) * np.sqrt(norm)
        
        return Si_list[0] - S_root

    def get_symmetry_contributions(self, nu: np.array) -> float:
        """
        """
        nu = np.asarray(nu)
        coeff_mod =  (
            # coefficient vector whose signs we are modifying:
            self.coeff_vec *
            # sign flips from generator reconstruction:
            self.pauli_mult_signs *
            # sign flips from nu assignment:
            (-1)**np.count_nonzero(np.logical_and(self.G_indices==1, nu == -1), axis=1)
        )
        s0 = np.sum(coeff_mod[self.mask_S0]).real
        si = np.array([np.sum(coeff_mod[mask]).real for mask in self.mask_Ci])
        return s0, si

    def get_energy(self, nu: np.array) -> float:
        """ The classical objective function that encodes the noncontextual energies
        """
        s0, si = self.get_symmetry_contributions(nu)
        return s0 - np.linalg.norm(si)
    
    def update_clique_representative_operator(self, clique_index:int = None) -> List[Tuple[PauliwordOp, float]]:
        _, si = self.get_symmetry_contributions(self.symmetry_generators.coeff_vec)
        self.clique_operator.coeff_vec = si
        if clique_index is None:
            clique_index = 0
        (
            self.mapped_clique_rep, 
            self.unitary_partitioning_rotations, 
            self.clique_normalization,
            self.clique_operator
        ) = self.clique_operator.unitary_partitioning(up_method=self.up_method, s_index=clique_index)
        
    def solve(self, 
            strategy: str = 'brute_force', 
            ref_state: np.array = None, 
            num_anneals:int = 1_000
        ) -> None:
        """ Minimize the classical objective function, yielding the noncontextual 
        ground state. This updates the coefficients of the clique representative 
        operator C(r) and symmetry generators G with the optimal configuration.

        Note most QUSO functions/methods work faster than their PUSO counterparts.

        """
        if ref_state is not None:
            # update the symmetry generator G coefficients w.r.t. the reference state
            self.symmetry_generators.update_sector(ref_state)
            ev_assignment = self.symmetry_generators.coeff_vec
            fixed_ev_mask = ev_assignment!=0
            fixed_eigvals = (ev_assignment[fixed_ev_mask]).astype(int)
            NC_solver = NoncontextualSolver(self, fixed_ev_mask, fixed_eigvals)
            # any remaining unfixed symmetry generators are solved via other means:
        else:
            NC_solver = NoncontextualSolver(self)

        NC_solver.num_anneals = num_anneals

        if strategy=='brute_force':
            self.energy, nu = NC_solver.energy_via_brute_force()

        elif strategy=='binary_relaxation':
            self.energy, nu = NC_solver.energy_via_relaxation()
        
        else:
            #### qubovert strategies below this point ####
            # PUSO = Polynomial unconstrained spin Optimization
            # QUSO: Quadratic Unconstrained Spin Optimization
            if strategy == 'brute_force_PUSO':
                NC_solver.method = 'brute_force'
                NC_solver.x = 'P'   
            elif strategy == 'brute_force_QUSO':  
                NC_solver.method = 'brute_force'
                NC_solver.x = 'Q'
            elif strategy == 'annealing_PUSO':
                NC_solver.method = 'annealing'
                NC_solver.x = 'P'
            elif strategy == 'annealing_QUSO':
                NC_solver.method = 'annealing'
                NC_solver.x = 'Q'
            else:
                raise ValueError(f'Unknown optimization strategy: {strategy}')
        
            self.energy, nu = NC_solver.energy_xUSO()

        # optimize the clique operator coefficients
        self.symmetry_generators.coeff_vec = nu.astype(int)
        self.update_clique_representative_operator()
        
    def get_qaoa(self, ref_state:QuantumState=None, type='qubo') -> dict:
        """
        For a given PUBO / QUBO problem make the following replacement:

         𝑥_𝑖 <--> (𝐼−𝑍_𝑖) / 2

         This defined the QAOA Hamiltonian
        Args:
            ref_state (optional): optional QuantumState to fix symmetry generators with
        Returns:
            QAOA_dict (dict): Dictionary of different QAOA Hamiltonians from discrete r_vectors

        """
        assert type in ['qubo', 'pubo']

        # fix symm generators if reference state given
        if ref_state is not None:
            # update the symmetry generator G coefficients w.r.t. the reference state
            self.symmetry_generators.update_sector(ref_state)
            ev_assignment = self.symmetry_generators.coeff_vec
            fixed_ev_mask = ev_assignment!=0
            fixed_eigvals = (ev_assignment[fixed_ev_mask]).astype(int)
        else:
            fixed_ev_mask = np.zeros(self.symmetry_generators.n_terms, dtype=bool)
            fixed_eigvals = np.array([], dtype=int)


        fixed_indices = np.where(fixed_ev_mask)[0]  # bool to indices
        fixed_assignments = dict(zip(fixed_indices, fixed_eigvals))

        r_vec_size = self.clique_operator.n_terms
        QAOA_dict = {}
        for j in range(r_vec_size):

            ## set extreme values of r_vec
            r_vec = np.zeros(r_vec_size)
            r_vec[j]=1

            r_part = np.sum(self.C_indices * r_vec, axis=1)
            r_part[~np.any(self.C_indices, axis=1)] = 1  # set all zero terms to 1 (aka multiply be value of 1)

            # setup spin
            q_vec_SPIN = {}
            for ind in range(self.symmetry_generators.n_terms):
                if ind in fixed_assignments.keys():
                    q_vec_SPIN[ind] = fixed_assignments[ind]
                else:
                    q_vec_SPIN[ind] = qv.spin_var('x%d' % ind)


            ## setup cost function
            COST = 0
            for P_index, term in enumerate(self.G_indices):
                non_zero_inds = term.nonzero()[0]
                # collect all the spin terms
                G_term = 1
                for i in non_zero_inds:
                    G_term *= q_vec_SPIN[i]

                COST += G_term * self.coeff_vec[P_index].real * self.pauli_mult_signs[P_index] * r_part[P_index].real

            if not isinstance(COST, qv._pcso.PCSO):
                # no degrees of freedom... Cost function is just Identity term
                QAOA_dict[j] = {
                    'r_vec': r_vec,
                    'H':PauliwordOp.from_dictionary({'I'*self.n_qubits: COST}),
                    'qubo': None
                }
            else:

                # make a spin/binary problem
                if type == 'qubo':
                    QUBO_problem = COST.to_qubo()
                elif type == 'pubo':
                    QUBO_problem = COST.to_pubo()
                else:
                    raise ValueError(f'unknown tspin problem: {type}')

                # note mapping often requires more qubits!
                QAOA_n_qubits = QUBO_problem.max_index+1

                I_string = 'I' * QAOA_n_qubits
                QAOA_H = PauliwordOp.empty(QAOA_n_qubits)
                for spin_inds, coeff in QUBO_problem.items():

                    if len(spin_inds) == 0:
                        QAOA_H += PauliwordOp.from_dictionary({I_string: coeff})
                    else:
                        temp = PauliwordOp.from_dictionary({I_string: coeff})
                        for q_ind in spin_inds:
                            op = list(I_string)
                            op[q_ind] = 'Z'
                            op_str = ''.join(op)
                            Qop = PauliwordOp.from_dictionary({I_string: 0.5,
                                                               op_str: -0.5})
                            temp *= Qop

                        QAOA_H += temp

                QAOA_dict[j] = {
                    'r_vec': r_vec,
                    'H': QAOA_H.copy(),
                    'qubo': dict(QUBO_problem.items())
                }

        return QAOA_dict

###############################################################################
################### NONCONTEXTUAL SOLVERS BELOW ###############################
###############################################################################

class NoncontextualSolver:

    # xUSO settings
    method:str = 'brute_force'
    x:str = 'P'
    num_anneals:int = 1_000,
    _nu = None

    def __init__(
        self,
        NC_op: NoncontextualOp,
        fixed_ev_mask: np.array = None,
        fixed_eigvals: np.array = None
        ) -> None:
        self.NC_op = NC_op
        
        if fixed_ev_mask is not None:
            assert fixed_eigvals is not None, 'Must specify the fixed eigenvalues'
            assert np.sum(fixed_ev_mask) == len(fixed_eigvals), 'Number of non-zero elements in mask does not match the number of fixed eigenvalues'
            self.fixed_ev_mask = fixed_ev_mask
            self.fixed_eigvals = fixed_eigvals
        else:
            self.fixed_ev_mask = np.zeros(NC_op.symmetry_generators.n_terms, dtype=bool)
            self.fixed_eigvals = np.array([], dtype=int)
    
    #################################################################
    ########################## BRUTE FORCE ##########################
    #################################################################

    def energy_via_brute_force(self) -> Tuple[float, np.array, np.array]:
        """ Does what is says on the tin! Try every single eigenvalue assignment in parallel
        and return the minimizing noncontextual configuration. This scales exponentially in 
        the number of unassigned symmetry elements.
        """
        if np.all(self.fixed_ev_mask):
            nu_list = self.fixed_eigvals.reshape([1,-1])
        else:
            search_size = 2**np.sum(~self.fixed_ev_mask)
            nu_list = np.ones([search_size, self.NC_op.symmetry_generators.n_terms], dtype=int)
            nu_list[:,self.fixed_ev_mask] = np.tile(self.fixed_eigvals, [search_size,1])
            nu_list[:,~self.fixed_ev_mask] = np.array(list(itertools.product([-1,1],repeat=np.sum(~self.fixed_ev_mask))))
        
        # # optimize over all discrete value assignments of nu in parallel
        # nu_chunks = [nu_list[i*n_cpu:(i+1)*n_cpu, :] for i in range(int(np.ceil(nu_list.shape[0] / n_cpu)))]
        with mp.Pool(mp.cpu_count()) as pool:    
            tracker = pool.map(self.NC_op.get_energy, nu_list)
        
        # find the lowest energy eigenvalue assignment from the full list
        full_search_results = zip(tracker, nu_list)
        energy, fixed_nu = min(full_search_results, key=lambda x:x[0])

        return energy, fixed_nu

    #################################################################
    ###################### BINARY RELAXATION ########################
    #################################################################

    def energy_via_relaxation(self) -> Tuple[float, np.array, np.array]:
        """ Relax the binary value assignment of symmetry generators to continuous variables
        """
        # optimize discrete value assignments nu by relaxation to continuous variables
        nu_bounds = [(0, np.pi)]*(self.NC_op.symmetry_generators.n_terms-np.sum(self.fixed_ev_mask))

        def get_nu(angles):
            """ Build nu vector given fixed values
            """
            nu = np.ones(self.NC_op.symmetry_generators.n_terms)
            nu[self.fixed_ev_mask] = self.fixed_eigvals
            nu[~self.fixed_ev_mask] = np.cos(angles)
            return nu

        optimizer_output = shgo(func=lambda angles:self.NC_op.get_energy(get_nu(angles)), bounds=nu_bounds)
        # if optimization was successful the optimal angles should consist of 0 and pi
        fix_nu = np.sign(np.array(get_nu(np.cos(optimizer_output['x'])))).astype(int)
        self.NC_op.symmetry_generators.coeff_vec = fix_nu 
        return optimizer_output['fun'], fix_nu
    
    #################################################################
    ################ UNCONSTRAINED SPIN OPTIMIZATION ################
    #################################################################    

    def get_cost_func(self):
        """ Define the unconstrained spin cost function
        """
        G_indices, _ = self.NC_op.symmetrized_operator.generator_reconstruction(self.NC_op.symmetry_generators)
        # setup spin variables
        fixed_indices = np.where(self.fixed_ev_mask)[0] # bool to indices
        fixed_assignments = dict(zip(fixed_indices, self.fixed_eigvals))
        q_vec_SPIN={}
        for ind in range(self.NC_op.symmetry_generators.n_terms):
            if ind in fixed_assignments.keys():
                q_vec_SPIN[ind] = fixed_assignments[ind]
            else:
                q_vec_SPIN[ind] = qv.spin_var('x%d' % ind)

        COST = 0
        for P_index, term in enumerate(G_indices):
            non_zero_inds = term.nonzero()[0]
            # collect all the spin terms
            G_term = 1
            for i in non_zero_inds:
                G_term *= q_vec_SPIN[i]

            # cost function
            COST += (
                G_term * 
                self.NC_op.symmetrized_operator.coeff_vec[P_index].real
                #self.NC_op.pauli_mult_signs[P_index]# * 
                #r_part[P_index].real
            )

        return COST

    def energy_xUSO(self) -> Tuple[float, np.array, np.array]:
        """
        Get energy via either: Polynomial unconstrained spin Optimization (x=P)
                                    or
                                Quadratic Unconstrained Spin Optimization  (x=Q)

        via a brute force search over q_vector or via simulated annealing

        Note in this method the r_vector is fixed upon input! (aka just does binary optimization)

        Args:
            NC_op (NoncontextualOp): noncontextual operator
            r_vec (np.array): array of clique expectation values <r_i>
            fixed_ev_mask (np.array): bool list of where eigenvalues in nu vector are fixed
            fixed_eigvals (np.array): list of nu eigenvalues that are fixed
            method (str): brute force or annealing optimization
            x (str): Whether method is Polynomial or Quadratic optimization
            num_anneals (optional): number of simulated anneals to do

        Returns:
            energy (float): noncontextual energy

        """
        assert self.x in ['P', 'Q']
        assert self.method in ['brute_force', 'annealing']
        
        COST = self.get_cost_func()
        
        if np.all(self.fixed_ev_mask):
            # if no degrees of freedom over nu vector, COST is a number
            nu_vec = self.fixed_eigvals
            energy = COST 
        else:
            if self.x =='P':
                spin_problem = COST.to_puso()
            else:
                spin_problem = COST.to_quso()

            if self.method=='brute_force':
                sol = spin_problem.solve_bruteforce()
            elif self.method == 'annealing':
                if self.x == 'P':
                    puso_res = qv.sim.anneal_puso(spin_problem, num_anneals=self.num_anneals)
                elif self.x == 'Q':
                    puso_res= qv.sim.anneal_quso(spin_problem, num_anneals=self.num_anneals)
                    assert COST.is_solution_valid(puso_res.best.state) is True
                sol = puso_res.best.state

            solution = COST.convert_solution(sol)
            nu_vec = np.ones(self.NC_op.symmetry_generators.n_terms, dtype=int)
            nu_vec[self.fixed_ev_mask] = self.fixed_eigvals
            # must ensure the binary variables are correctly ordered in the solution:
            nu_vec[~self.fixed_ev_mask] = np.array([solution[f'x{i}'] for i in range(COST.num_binary_variables)])
        
        return self.NC_op.get_energy(nu_vec), nu_vec