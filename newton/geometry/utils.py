# SPDX-FileCopyrightText: Copyright (c) 2025 The Newton Developers
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
import warnings
from collections import defaultdict
from typing import Literal

import numpy as np
import warp as wp

from ..core.types import Vec3, nparray
from .inertia import compute_mesh_inertia
from .types import (
    GEO_BOX,
    GEO_CAPSULE,
    GEO_CONE,
    GEO_CYLINDER,
    GEO_MESH,
    GEO_PLANE,
    GEO_SPHERE,
    SDF,
    Mesh,
)


def compute_shape_radius(geo_type: int, scale: Vec3, src: Mesh | SDF | None) -> float:
    """
    Calculates the radius of a sphere that encloses the shape, used for broadphase collision detection.
    """
    if geo_type == GEO_SPHERE:
        return scale[0]
    elif geo_type == GEO_BOX:
        return np.linalg.norm(scale)
    elif geo_type == GEO_CAPSULE or geo_type == GEO_CYLINDER or geo_type == GEO_CONE:
        return scale[0] + scale[1]
    elif geo_type == GEO_MESH:
        vmax = np.max(np.abs(src.vertices), axis=0) * np.max(scale)
        return np.linalg.norm(vmax)
    elif geo_type == GEO_PLANE:
        if scale[0] > 0.0 and scale[1] > 0.0:
            # finite plane
            return np.linalg.norm(scale)
        else:
            return 1.0e6
    else:
        return 10.0


def compute_aabb(vertices: nparray) -> tuple[Vec3, Vec3]:
    """Compute the axis-aligned bounding box of a set of vertices."""
    min_coords = np.min(vertices, axis=0)
    max_coords = np.max(vertices, axis=0)
    return min_coords, max_coords


def compute_obb(vertices: nparray) -> tuple[wp.transform, wp.vec3]:
    """Compute the oriented bounding box of a set of vertices.

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.

    Returns:
        A tuple containing:
        - transform: The transform of the oriented bounding box
        - extents: The half-extents of the box along its principal axes
    """
    if len(vertices) == 0:
        return wp.transform_identity(), wp.vec3(0.0, 0.0, 0.0)
    if len(vertices) == 1:
        return wp.transform(wp.vec3(vertices[0]), wp.quat_identity()), wp.vec3(0.0, 0.0, 0.0)

    # Center the vertices
    center = np.mean(vertices, axis=0)
    centered_vertices = vertices - center

    # Compute covariance matrix with handling for degenerate cases
    if len(vertices) < 3:
        # For 2 points, create a line-aligned OBB
        direction = centered_vertices[1] if len(vertices) > 1 else np.array([1, 0, 0])
        direction = direction / np.linalg.norm(direction) if np.linalg.norm(direction) > 1e-6 else np.array([1, 0, 0])
        # Create orthogonal basis
        if abs(direction[0]) < 0.9:
            perpendicular = np.cross(direction, [1, 0, 0])
        else:
            perpendicular = np.cross(direction, [0, 1, 0])
        perpendicular = perpendicular / np.linalg.norm(perpendicular)
        third = np.cross(direction, perpendicular)
        eigenvectors = np.column_stack([direction, perpendicular, third])
    else:
        cov_matrix = np.cov(centered_vertices.T)
        eigenvalues, eigenvectors = np.linalg.eigh(cov_matrix)
        # Sort by eigenvalues in descending order
        sorted_indices = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[sorted_indices]
        eigenvectors = eigenvectors[:, sorted_indices]

        # Ensure right-handed coordinate system
        if np.linalg.det(eigenvectors) < 0:
            eigenvectors[:, 2] *= -1

    # Project vertices onto principal axes
    projected = centered_vertices @ eigenvectors

    # Compute extents
    min_coords = np.min(projected, axis=0)
    max_coords = np.max(projected, axis=0)
    extents = (max_coords - min_coords) / 2.0

    # Calculate the center in the projected coordinate system
    center_offset = (max_coords + min_coords) / 2.0
    # Transform the center offset back to the original coordinate system
    center = center + center_offset @ eigenvectors.T

    # Convert rotation matrix to quaternion
    # The rotation matrix should transform from the original coordinate system to the principal axes
    # eigenvectors is the rotation matrix from original to principal axes
    rotation_matrix = eigenvectors

    # Convert to quaternion using Warp's quat_from_matrix function
    # First convert numpy array to Warp matrix
    orientation = wp.quat_from_matrix(wp.mat33(rotation_matrix))

    return wp.transform(wp.vec3(center), orientation), wp.vec3(extents)


def load_mesh(filename: str, method: str | None = None):
    """
    Loads a 3D triangular surface mesh from a file.

    Args:
        filename (str): The path to the 3D model file (obj, and other formats supported by the different methods) to load.
        method (str): The method to use for loading the mesh (default None). Can be either `"trimesh"`, `"meshio"`, `"pcu"`, or `"openmesh"`. If None, every method is tried and the first successful mesh import where the number of vertices is greater than 0 is returned.

    Returns:
        Tuple of (mesh_points, mesh_indices), where mesh_points is a Nx3 numpy array of vertex positions (float32),
        and mesh_indices is a Mx3 numpy array of vertex indices (int32) for the triangular faces.
    """
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File not found: {filename}")

    def load_mesh_with_method(method):
        if method == "meshio":
            import meshio  # noqa: PLC0415

            m = meshio.read(filename)
            mesh_points = np.array(m.points)
            mesh_indices = np.array(m.cells[0].data, dtype=np.int32)
        elif method == "openmesh":
            import openmesh  # noqa: PLC0415

            m = openmesh.read_trimesh(filename)
            mesh_points = np.array(m.points())
            mesh_indices = np.array(m.face_vertex_indices(), dtype=np.int32)
        elif method == "pcu":
            import point_cloud_utils as pcu  # noqa: PLC0415

            mesh_points, mesh_indices = pcu.load_mesh_vf(filename)
            mesh_indices = mesh_indices.flatten()
        else:
            import trimesh  # noqa: PLC0415

            m = trimesh.load(filename)
            if hasattr(m, "geometry"):
                # multiple meshes are contained in a scene; combine to one mesh
                mesh_points = []
                mesh_indices = []
                index_offset = 0
                for geom in m.geometry.values():
                    vertices = np.array(geom.vertices, dtype=np.float32)
                    faces = np.array(geom.faces.flatten(), dtype=np.int32)
                    mesh_points.append(vertices)
                    mesh_indices.append(faces + index_offset)
                    index_offset += len(vertices)
                mesh_points = np.concatenate(mesh_points, axis=0)
                mesh_indices = np.concatenate(mesh_indices)
            else:
                # a single mesh
                mesh_points = np.array(m.vertices, dtype=np.float32)
                mesh_indices = np.array(m.faces.flatten(), dtype=np.int32)
        return mesh_points, mesh_indices

    if method is None:
        methods = ["trimesh", "meshio", "pcu", "openmesh"]
        for method in methods:
            try:
                mesh = load_mesh_with_method(method)
                if mesh is not None and len(mesh[0]) > 0:
                    return mesh
            except Exception:
                pass
        raise ValueError(f"Failed to load mesh using any of the methods: {methods}")
    else:
        mesh = load_mesh_with_method(method)
        if mesh is None or len(mesh[0]) == 0:
            raise ValueError(f"Failed to load mesh using method {method}")
        return mesh


def visualize_meshes(
    meshes: list[tuple[list, list]], num_cols=0, num_rows=0, titles=None, scale_axes=True, show_plot=True
):
    """Render meshes in a grid with matplotlib."""

    import matplotlib.pyplot as plt  # noqa: PLC0415

    if titles is None:
        titles = []

    num_cols = min(num_cols, len(meshes))
    num_rows = min(num_rows, len(meshes))
    if num_cols and not num_rows:
        num_rows = int(np.ceil(len(meshes) / num_cols))
    elif num_rows and not num_cols:
        num_cols = int(np.ceil(len(meshes) / num_rows))
    else:
        num_cols = len(meshes)
        num_rows = 1

    vertices = [np.array(v).reshape((-1, 3)) for v, _ in meshes]
    faces = [np.array(f, dtype=np.int32).reshape((-1, 3)) for _, f in meshes]
    if scale_axes:
        ranges = np.array([v.max(axis=0) - v.min(axis=0) for v in vertices])
        max_range = ranges.max()
        mid_points = np.array([v.max(axis=0) + v.min(axis=0) for v in vertices]) * 0.5

    fig = plt.figure(figsize=(12, 6))
    for i, (vertices, faces) in enumerate(meshes):
        ax = fig.add_subplot(num_rows, num_cols, i + 1, projection="3d")
        if i < len(titles):
            ax.set_title(titles[i])
        ax.plot_trisurf(vertices[:, 0], vertices[:, 1], vertices[:, 2], triangles=faces, edgecolor="k")
        if scale_axes:
            mid = mid_points[i]
            ax.set_xlim(mid[0] - max_range, mid[0] + max_range)
            ax.set_ylim(mid[1] - max_range, mid[1] + max_range)
            ax.set_zlim(mid[2] - max_range, mid[2] + max_range)
    if show_plot:
        plt.show()
    return fig


@contextlib.contextmanager
def silence_stdio():
    """
    Redirect *both* Python-level and C-level stdout/stderr to os.devnull
    for the duration of the with-block.
    """
    devnull = open(os.devnull, "w")
    # Duplicate the real fds so we can restore them later
    old_stdout_fd = os.dup(1)
    old_stderr_fd = os.dup(2)

    try:
        # Point fds 1 and 2 at /dev/null
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

        # Also patch the Python objects that wrap those fds
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            yield
    finally:
        # Restore original fds
        os.dup2(old_stdout_fd, 1)
        os.dup2(old_stderr_fd, 2)
        os.close(old_stdout_fd)
        os.close(old_stderr_fd)
        devnull.close()


def remesh_ftetwild(vertices, faces, optimize=False, edge_length_fac=0.05, verbose=False):
    """Remesh a 3D triangular surface mesh using "Fast Tetrahedral Meshing in the Wild" (fTetWild).

    This is useful for improving the quality of the mesh, and for ensuring that the mesh is
    watertight. This function first tetrahedralizes the mesh, then extracts the surface mesh.
    The resulting mesh is guaranteed to be watertight and may have a different topology than the
    input mesh.

    Uses pytetwild, a Python wrapper for fTetWild, to perform the remeshing.
    See https://github.com/pyvista/pytetwild.

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.
        faces: A numpy array of shape (M, 3) containing the vertex indices of the faces.
        optimize: Whether to optimize the mesh quality during remeshing.
        edge_length_fac: The target edge length of the tetrahedral element as a fraction of the bounding box diagonal.

    Returns:
        A tuple (vertices, faces) containing the remeshed mesh. Returns the original vertices and faces
        if the remeshing fails.
    """

    from pytetwild import tetrahedralize  # noqa: PLC0415

    def tet_fn(v, f):
        return tetrahedralize(v, f, optimize=optimize, edge_length_fac=edge_length_fac)

    if verbose:
        tet_vertices, tet_indices = tet_fn(vertices, faces)
    else:
        # Suppress stdout and stderr during tetrahedralize
        with silence_stdio():
            tet_vertices, tet_indices = tet_fn(vertices, faces)

    def face_indices(tet):
        face1 = (tet[0], tet[2], tet[1])
        face2 = (tet[1], tet[2], tet[3])
        face3 = (tet[0], tet[1], tet[3])
        face4 = (tet[0], tet[3], tet[2])
        return (
            (face1, tuple(sorted(face1))),
            (face2, tuple(sorted(face2))),
            (face3, tuple(sorted(face3))),
            (face4, tuple(sorted(face4))),
        )

    # determine surface faces
    elements_per_face = defaultdict(set)
    unique_faces = {}
    for e, tet in enumerate(tet_indices):
        for face, key in face_indices(tet):
            elements_per_face[key].add(e)
            unique_faces[key] = face
    surface_faces = [face for key, face in unique_faces.items() if len(elements_per_face[key]) == 1]

    new_vertices = np.array(tet_vertices)
    new_faces = np.array(surface_faces, dtype=np.int32)

    if len(new_vertices) == 0 or len(new_faces) == 0:
        warnings.warn(
            "Remeshing failed, the optimized mesh has no vertices or faces; return previous mesh.", stacklevel=2
        )
        return vertices, faces

    return new_vertices, new_faces


def remesh_alphashape(vertices, alpha: float = 3.0):
    """Remesh a 3D triangular surface mesh using the alpha shape algorithm.

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.
        faces: A numpy array of shape (M, 3) containing the vertex indices of the faces (not needed).
        alpha: The alpha shape parameter.

    Returns:
        A tuple (vertices, faces) containing the remeshed mesh.
    """
    import alphashape  # noqa: PLC0415

    with silence_stdio():
        alpha_shape = alphashape.alphashape(vertices, alpha)
    return np.array(alpha_shape.vertices), np.array(alpha_shape.faces, dtype=np.int32)


def remesh_quadratic(vertices, faces, target_reduction=0.5, target_count=None, **kwargs):
    """Remesh a 3D triangular surface mesh using fast quadratic mesh simplification.

    https://github.com/pyvista/fast-simplification

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.
        faces: A numpy array of shape (M, 3) containing the vertex indices of the faces.
        target_reduction: The target reduction factor for the number of faces (0.0 to 1.0).
        **kwargs: Additional keyword arguments for the remeshing algorithm.

    Returns:
        A tuple (vertices, faces) containing the remeshed mesh.
    """
    from fast_simplification import simplify  # noqa: PLC0415

    return simplify(vertices, faces, target_reduction=target_reduction, target_count=target_count, **kwargs)


def remesh_convex_hull(vertices, maxhullvert: int = 0):
    """Compute the convex hull of a set of 3D points and return the vertices and faces of the convex hull mesh.

    Uses ``scipy.spatial.ConvexHull`` to compute the convex hull.

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.
        maxhullvert: The maximum number of vertices for the convex hull. If 0, no limit is applied.

    Returns:
        A tuple (verts, faces) where:
        - verts: A numpy array of shape (M, 3) containing the vertex positions of the convex hull.
        - faces: A numpy array of shape (K, 3) containing the vertex indices of the triangular faces of the convex hull.
    """

    from scipy.spatial import ConvexHull  # noqa: PLC0415

    qhull_options = "Qt"
    if maxhullvert > 0:
        # qhull "TA" actually means "number of vertices added after the initial simplex"
        # from mujoco's user_mesh.cc
        qhull_options += f" TA{maxhullvert - 4}"
    hull = ConvexHull(vertices, qhull_options=qhull_options)
    verts = hull.points.copy().astype(np.float32)
    faces = hull.simplices.astype(np.int32)

    # fix winding order of faces
    centre = verts.mean(0)
    for i, tri in enumerate(faces):
        a, b, c = verts[tri]
        normal = np.cross(b - a, c - a)
        if np.dot(normal, a - centre) < 0:
            faces[i] = tri[[0, 2, 1]]

    # trim vertices to only those that are used in the faces
    unique_verts = np.unique(faces.flatten())
    verts = verts[unique_verts]
    # update face indices to use the new vertex indices
    mapping = {v: i for i, v in enumerate(unique_verts)}
    faces = np.array([mapping[v] for v in faces.flatten()], dtype=np.int32).reshape(faces.shape)

    return verts, faces


RemeshingMethod = Literal["ftetwild", "alphashape", "quadratic", "convex_hull"]


def remesh(
    vertices, faces, method: RemeshingMethod = "quadratic", visualize=False, **remeshing_kwargs
) -> tuple[nparray, nparray]:
    """
    Remeshes a 3D triangular surface mesh using the specified method.

    Args:
        vertices: A numpy array of shape (N, 3) containing the vertex positions.
        faces: A numpy array of shape (M, 3) containing the vertex indices of the faces.
        method: The remeshing method to use. One of "ftetwild", "quadratic", "convex_hull", or "alphashape".
        visualize: Whether to render the input and output meshes using matplotlib.
        **remeshing_kwargs: Additional keyword arguments passed to the remeshing function.

    Returns:
        A tuple (vertices, faces) containing the remeshed mesh.
    """
    if method == "ftetwild":
        new_vertices, new_faces = remesh_ftetwild(vertices, faces, **remeshing_kwargs)
    elif method == "alphashape":
        new_vertices, new_faces = remesh_alphashape(vertices, **remeshing_kwargs)
    elif method == "quadratic":
        new_vertices, new_faces = remesh_quadratic(vertices, faces, **remeshing_kwargs)
    elif method == "convex_hull":
        new_vertices, new_faces = remesh_convex_hull(vertices, **remeshing_kwargs)
    else:
        raise ValueError(f"Unknown remeshing method: {method}")

    if visualize:
        # side-by-side visualization of the input and output meshes
        visualize_meshes(
            [(vertices, faces), (new_vertices, new_faces)],
            titles=[
                f"Original ({len(vertices)} verts, {len(faces)} faces)",
                f"Remeshed ({len(new_vertices)} verts, {len(new_faces)} faces)",
            ],
        )
    return new_vertices, new_faces


def remesh_mesh(
    mesh: Mesh,
    method: RemeshingMethod = "quadratic",
    recompute_inertia: bool = False,
    inplace: bool = False,
    **remeshing_kwargs,
) -> Mesh:
    """Remesh a mesh using the specified method.
    Args:
        mesh: The mesh to remesh.
        method: The remeshing method to use. One of "ftetwild", "quadratic", "convex_hull", or "alphashape".
        recompute_inertia: Whether to recompute the inertia of the mesh.
        inplace: Whether to modify the mesh in place or return a new mesh.
        **remeshing_kwargs: Additional keyword arguments passed to the remeshing function.
    Returns:
        The remeshed mesh.
    """
    if method == "convex_hull":
        remeshing_kwargs["maxhullvert"] = mesh.maxhullvert
    vertices, indices = remesh(mesh.vertices, mesh.indices.reshape(-1, 3), method=method, **remeshing_kwargs)
    if inplace:
        mesh.vertices = vertices
        mesh.indices = indices.flatten()
        if recompute_inertia:
            mesh.mass, mesh.com, mesh.I, _ = compute_mesh_inertia(1.0, vertices, indices, is_solid=mesh.is_solid)
    else:
        return mesh.copy(vertices=vertices, indices=indices, recompute_inertia=recompute_inertia)
    return mesh


def create_box_mesh(half_extents: Vec3) -> tuple[nparray, nparray]:
    x_extent, y_extent, z_extent = half_extents
    vertices = np.array(
        [
            [-x_extent, -y_extent, -z_extent],
            [x_extent, -y_extent, -z_extent],
            [x_extent, y_extent, -z_extent],
            [-x_extent, y_extent, -z_extent],
            [-x_extent, -y_extent, z_extent],
            [x_extent, -y_extent, z_extent],
            [x_extent, y_extent, z_extent],
            [-x_extent, y_extent, z_extent],
        ],
        dtype=np.float32,
    )
    indices = np.array(
        [
            # Bottom face (z = -z_extent)
            0,
            2,
            1,
            0,
            3,
            2,
            # Top face (z = z_extent)
            4,
            5,
            6,
            4,
            6,
            7,
            # Front face (y = -y_extent)
            0,
            1,
            5,
            0,
            5,
            4,
            # Back face (y = y_extent)
            2,
            3,
            7,
            2,
            7,
            6,
            # Left face (x = -x_extent)
            0,
            4,
            7,
            0,
            7,
            3,
            # Right face (x = x_extent)
            1,
            2,
            6,
            1,
            6,
            5,
        ],
        dtype=np.int32,
    )
    return vertices, indices


def transform_points(points: nparray, transform: wp.transform, scale: Vec3 | None = None) -> nparray:
    if scale is not None:
        points = points * np.array(scale, dtype=np.float32)
    return points @ np.array(wp.quat_to_matrix(transform.q)).reshape(3, 3) + transform.p


__all__ = ["compute_shape_radius", "load_mesh", "visualize_meshes"]
